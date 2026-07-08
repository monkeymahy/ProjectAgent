from __future__ import annotations

import enum
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.core.config import settings

DB_PATH = settings.storage_dir / "projshow.db"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    CLONING = "cloning"
    PARSING = "parsing"
    GENERATING = "generating"
    DONE = "done"
    FAILED = "failed"


def _connect() -> sqlite3.Connection:
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,        -- url | zip | local
                source TEXT NOT NULL,
                owner_name TEXT NOT NULL DEFAULT '匿名',
                owner_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                progress INTEGER NOT NULL DEFAULT 0,
                message TEXT,
                parsed_json TEXT,
                generated_json TEXT,
                html_path TEXT,
                template_version INTEGER NOT NULL DEFAULT 1,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # 兼容旧库：补缺失列
        cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
        if "owner_name" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN owner_name TEXT NOT NULL DEFAULT '匿名'")
        if "owner_id" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN owner_id INTEGER")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                tforum_user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                account TEXT,
                avatar TEXT,
                role TEXT,
                email TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS project_cards (
                project_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                primary_lang TEXT,
                lang_json TEXT NOT NULL DEFAULT '{}',
                tech_stack_json TEXT NOT NULL DEFAULT '[]',
                tags_json TEXT NOT NULL DEFAULT '[]',
                card_color TEXT NOT NULL DEFAULT '#58a6ff',
                owner_name TEXT NOT NULL DEFAULT '匿名',
                owner_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id)
            )
            """
        )
        # 兼容旧库：project_cards 补 owner_id 列
        card_cols = {r[1] for r in conn.execute("PRAGMA table_info(project_cards)").fetchall()}
        if "owner_id" not in card_cols:
            conn.execute("ALTER TABLE project_cards ADD COLUMN owner_id INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cards_created ON project_cards(created_at DESC)"
        )


def create_project(
    project_id: str,
    source_type: str,
    source: str,
    owner_name: str = "匿名",
    owner_id: Optional[int] = None,
) -> None:
    now = _now()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO projects(id, source_type, source, owner_name, owner_id, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (project_id, source_type, source, owner_name or "匿名", owner_id,
             TaskStatus.PENDING.value, now, now),
        )


def get_project(project_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return dict(row) if row else None


def update_status(
    project_id: str,
    status: TaskStatus,
    progress: int,
    message: str = "",
    **fields,
) -> None:
    now = _now()
    sets = ["status=?", "progress=?", "message=?", "updated_at=?"]
    vals: list = [status.value, progress, message, now]
    for k, v in fields.items():
        sets.append(f"{k}=?")
        vals.append(v if not isinstance(v, (dict, list)) else json.dumps(v, ensure_ascii=False))
    vals.append(project_id)
    with _connect() as conn:
        conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", vals)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_failed(project_id: str, error: str) -> None:
    update_status(project_id, TaskStatus.FAILED, -1, "failed", error=error)


# 语言 -> 卡片色（与展示页语言分布调色板保持一致）
LANG_COLORS = {
    "Python": "#3572a5", "JavaScript": "#f1e05a", "TypeScript": "#3178c6",
    "Java": "#b07219", "Go": "#00add8", "Rust": "#dea584", "C++": "#f34b7d",
    "C": "#555555", "C#": "#178600", "Ruby": "#701516", "PHP": "#4f5d95",
    "Swift": "#ffac45", "Kotlin": "#f18e33", "Shell": "#89e051",
    "HTML": "#e34c26", "CSS": "#563d7c", "Vue": "#41b883",
    "Jupyter Notebook": "#da5b0b", "Markdown": "#083fa1", "JSON": "#292929",
}


def _clean_text(s: str) -> str:
    """剥掉 HTML 标签、压缩空白，防止 LLM 把 README 的 <div> 当 title 入库。"""
    if not s:
        return ""
    import re
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def upsert_card(
    project_id: str,
    parsed: dict,
    generated: dict,
    owner_name: str = "匿名",
    owner_id: Optional[int] = None,
) -> None:
    """生成完成后，把卡片摘要字段平铺写入 project_cards。"""
    languages = parsed.get("languages", {}) or {}
    primary_lang = next(iter(languages), "") if languages else ""
    card_color = LANG_COLORS.get(primary_lang, "#58a6ff")
    created_at = _now()

    # 用 projects 表里已有的 created_at，保持列表顺序和提交时间一致
    with _connect() as conn:
        row = conn.execute("SELECT created_at, owner_id FROM projects WHERE id=?", (project_id,)).fetchone()
        if row:
            created_at = row["created_at"]
            if owner_id is None and row["owner_id"] is not None:
                owner_id = row["owner_id"]

    title = _clean_text(generated.get("title") or parsed.get("name") or "未命名项目")[:120]
    summary = _clean_text(generated.get("one_line_summary", ""))[:300]

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO project_cards(
                project_id, title, summary, primary_lang, lang_json,
                tech_stack_json, tags_json, card_color, owner_name, owner_id, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(project_id) DO UPDATE SET
                title=excluded.title, summary=excluded.summary,
                primary_lang=excluded.primary_lang, lang_json=excluded.lang_json,
                tech_stack_json=excluded.tech_stack_json, tags_json=excluded.tags_json,
                card_color=excluded.card_color, owner_name=excluded.owner_name,
                owner_id=excluded.owner_id
            """,
            (
                project_id,
                title,
                summary,
                primary_lang,
                json.dumps(languages, ensure_ascii=False),
                json.dumps(generated.get("tech_stack", []) or [], ensure_ascii=False),
                json.dumps(generated.get("tags", []) or [], ensure_ascii=False),
                card_color,
                owner_name or "匿名",
                owner_id,
                created_at,
            ),
        )


def list_cards(
    page: int = 1,
    per_page: int = 24,
    lang: Optional[str] = None,
    tag: Optional[str] = None,
) -> tuple[list[dict], int]:
    """分页查询卡片。返回 (cards, total)。按 created_at 倒序，只含 done 项目。"""
    where = ["p.status = ?"]
    params: list = [TaskStatus.DONE.value]
    if lang:
        where.append("c.primary_lang = ?")
        params.append(lang)
    if tag:
        where.append("c.tags_json LIKE ?")
        params.append(f'%"{tag}"%')
    clause = " AND ".join(where)

    offset = (max(1, page) - 1) * per_page
    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM project_cards c "
            f"JOIN projects p ON p.id = c.project_id WHERE {clause}",
            params,
        ).fetchone()["n"]

        rows = conn.execute(
            f"""
            SELECT c.*, p.status FROM project_cards c
            JOIN projects p ON p.id = c.project_id
            WHERE {clause}
            ORDER BY c.created_at DESC
            LIMIT ? OFFSET ?
            """,
            params + [per_page, offset],
        ).fetchall()

    cards = []
    for r in rows:
        cards.append({
            "project_id": r["project_id"],
            "title": r["title"],
            "summary": r["summary"],
            "primary_lang": r["primary_lang"],
            "languages": json.loads(r["lang_json"] or "{}"),
            "tech_stack": json.loads(r["tech_stack_json"] or "[]"),
            "tags": json.loads(r["tags_json"] or "[]"),
            "card_color": r["card_color"],
            "owner_name": r["owner_name"],
            "owner_id": r["owner_id"],
            "created_at": r["created_at"],
        })
    return cards, total


def upsert_user(info: dict) -> dict:
    """SSO 校验通过后，把 tForum 用户信息写入 users 表。返回用户 dict。"""
    uid = int(info.get("id"))
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users(tforum_user_id, username, account, avatar, role, email,
                              first_seen_at, last_seen_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(tforum_user_id) DO UPDATE SET
                username=excluded.username, account=excluded.account,
                avatar=excluded.avatar, role=excluded.role, email=excluded.email,
                last_seen_at=excluded.last_seen_at
            """,
            (
                uid,
                info.get("username") or info.get("account") or f"user_{uid}",
                info.get("account"),
                info.get("avatar"),
                info.get("role"),
                info.get("email"),
                now, now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM users WHERE tforum_user_id=?", (uid,)
        ).fetchone()
    return dict(row)


def get_user(tforum_user_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE tforum_user_id=?", (tforum_user_id,)
        ).fetchone()
        return dict(row) if row else None


def distinct_filter_values(field: str) -> list[str]:
    """获取筛选可选项：lang 或 tag。"""
    if field == "lang":
        with _connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT primary_lang FROM project_cards "
                "WHERE primary_lang != '' ORDER BY primary_lang"
            ).fetchall()
        return [r["primary_lang"] for r in rows]
    if field == "tag":
        tags: set[str] = set()
        with _connect() as conn:
            rows = conn.execute("SELECT tags_json FROM project_cards").fetchall()
        for r in rows:
            for t in json.loads(r["tags_json"] or "[]"):
                tags.add(t)
        return sorted(tags)
    return []
