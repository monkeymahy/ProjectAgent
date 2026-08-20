"""解析工具库 / 技能库仓库，输出统一的条目列表。

两个仓库的数据格式未完全契约化，这里做容错解析：
- 优先读 registry.json（支持 list / {"tools": [...]} / {name: {...}} 等形状）
- 工具库回退解析分类目录下的 *.md（cad/nx.md 等）的 YAML frontmatter
- 技能库回退解析 skills/**/LINK.yaml（目录名即技能名）
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)

# 工具条目里允许透传到前端的字段（其余进 extra）
TOOL_FIELDS = (
    "name", "type", "category", "license", "difficulty", "ai_friendly",
    "pricing_model", "tags", "self_developed", "official_url", "repo_url",
    "screenshot_url", "platforms", "author", "description", "scenarios",
    "usage", "pros", "cons", "ai_agent", "official_docs", "last_verified",
)
SKILL_FIELDS = (
    "name", "description", "domain", "skillType", "skill_type", "tags",
    "gitUrl", "git_url", "blogUrl", "blog_url", "authorName", "author_name",
    "phase", "path", "isOriginal", "is_original", "status",
)


def parse_repo(source: str, repo_dir: Path) -> list[dict]:
    if source == "tool":
        return _parse_tool(repo_dir)
    return _parse_skill(repo_dir)


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("解析 %s 失败: %s", path, e)
        return None


def _read_frontmatter(path: Path) -> Optional[dict]:
    """读 md 文件的 YAML frontmatter（--- 包裹）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        data = yaml.safe_load(parts[1])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _json_items(reg) -> list[dict]:
    """registry.json 形状容错：list / {"tools"/"skills": [...]} / {name: {...}}。"""
    if isinstance(reg, list):
        return [x for x in reg if isinstance(x, dict)]
    if isinstance(reg, dict):
        for key in ("tools", "skills", "entries", "items"):
            if isinstance(reg.get(key), list):
                return [x for x in reg[key] if isinstance(x, dict)]
        return [v for v in reg.values() if isinstance(v, dict)]
    return []


def _pick(entry: dict, keys: tuple) -> dict:
    """大小写/下划线不敏感地取已知字段。"""
    lower = {k.lower().replace("-", "_"): k for k in entry}
    out = {}
    for f in keys:
        k = lower.get(f.lower())
        if k is not None and entry.get(k) not in (None, "", []):
            out[f] = entry[k]
    return out


def _normalize(source: str, entry: dict, path: str = "") -> Optional[dict]:
    fields = _pick(entry, TOOL_FIELDS if source == "tool" else SKILL_FIELDS)
    name = fields.get("name")
    if not name:
        return None
    if source == "tool":
        repo_url = fields.get("repo_url", "")
        category = fields.get("category", "")
    else:
        repo_url = fields.get("gitUrl") or fields.get("git_url", "")
        category = fields.get("domain", "")
    extra = {k: v for k, v in entry.items() if k not in fields.values()}
    return {
        "source": source,
        "name": str(name),
        "category": category,
        "entry_type": fields.get("type") or fields.get("skillType") or fields.get("skill_type", ""),
        "description": str(fields.get("description", "") or ""),
        "tags": fields.get("tags", []) if isinstance(fields.get("tags"), list) else [],
        "license": fields.get("license", ""),
        "official_url": fields.get("official_url", "") if source == "tool" else "",
        "repo_url": repo_url or "",
        "author": fields.get("author") or fields.get("authorName") or fields.get("author_name", ""),
        "difficulty": fields.get("difficulty", ""),
        "ai_friendly": fields.get("ai_friendly", ""),
        "pricing_model": fields.get("pricing_model", ""),
        "path": path or fields.get("path", ""),
        "extra": extra,
    }


def _parse_tool(repo_dir: Path) -> list[dict]:
    entries: dict[str, dict] = {}

    for item in _json_items(_read_json(repo_dir / "registry.json")):
        norm = _normalize("tool", item)
        if norm:
            entries[norm["name"].lower()] = norm

    # 分类目录下的 *.md（cad/nx.md、fluid/xx.md、category/xx.md 等），
    # frontmatter 有 name 才算工具条目；目录名作分类兜底
    for md in repo_dir.glob("*/*.md"):
        fm = _read_frontmatter(md)
        if not fm or not fm.get("name"):
            continue
        if not any(k.lower() == "category" for k in fm):
            fm["category"] = md.parent.name
        norm = _normalize("tool", fm, path=md.relative_to(repo_dir).as_posix())
        if not norm:
            continue
        key = norm["name"].lower()
        if key in entries:
            # registry 已有：用 md 的字段补空缺
            for k, v in norm.items():
                if k not in ("extra",) and not entries[key].get(k):
                    entries[key][k] = v
            entries[key]["path"] = entries[key].get("path") or norm["path"]
        else:
            entries[key] = norm

    return list(entries.values())


def _parse_skill(repo_dir: Path) -> list[dict]:
    entries: dict[str, dict] = {}

    for item in _json_items(_read_json(repo_dir / "registry.json")):
        norm = _normalize("skill", item)
        if norm:
            entries[norm["name"].lower()] = norm

    if not entries:
        # 回退：skills/**/LINK.yaml，目录名即技能名
        skills_dir = repo_dir / "skills"
        if skills_dir.is_dir():
            for link in skills_dir.rglob("LINK.yaml"):
                try:
                    data = yaml.safe_load(link.read_text(encoding="utf-8"))
                except Exception:
                    data = None
                data = data if isinstance(data, dict) else {}
                rel = link.relative_to(repo_dir).as_posix()
                data.setdefault("name", link.parent.name)
                norm = _normalize("skill", data, path=rel)
                if norm:
                    entries[norm["name"].lower()] = norm

    return list(entries.values())
