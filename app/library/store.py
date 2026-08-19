"""库数据内存缓存 + 后台定时刷新。"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from app.core.config import settings
from app.library import git_sync, parser

log = logging.getLogger(__name__)

_lock = threading.Lock()
_entries: list[dict] = []
_loaded_at: str = ""
_last_error: str = ""
_refreshing = False


def is_configured() -> bool:
    return bool(settings.library_tool_repo_url or settings.library_skill_repo_url)


def _configured_repos() -> list[tuple[str, str, str, str, str, str]]:
    """[(source, 本地目录名, url, branch, user, token)]。"""
    repos = []
    if settings.library_tool_repo_url:
        repos.append((
            "tool", "tool-repo", settings.library_tool_repo_url,
            settings.library_tool_repo_branch,
            settings.library_tool_repo_user, settings.library_tool_repo_token,
        ))
    if settings.library_skill_repo_url:
        repos.append((
            "skill", "skill-repo", settings.library_skill_repo_url,
            settings.library_skill_repo_branch,
            settings.library_skill_repo_user, settings.library_skill_repo_token,
        ))
    return repos


def refresh() -> str:
    """拉取全部配置仓库并重建缓存，返回错误信息（空串为成功）。"""
    global _entries, _loaded_at, _last_error, _refreshing
    with _lock:
        if _refreshing:
            return _last_error
        _refreshing = True

    all_entries: list[dict] = []
    errors: list[str] = []
    try:
        for source, name, url, branch, user, token in _configured_repos():
            try:
                path = git_sync.ensure_repo(name, url, branch, user=user, token=token)
                all_entries.extend(parser.parse_repo(source, path))
            except Exception as e:
                log.warning("库仓库 %s 同步失败: %s", name, e)
                errors.append(f"{name}: {e}")
        with _lock:
            if all_entries or not errors:
                _entries = all_entries
            _loaded_at = datetime.now(timezone.utc).isoformat()
            _last_error = "; ".join(errors)
        return _last_error
    finally:
        with _lock:
            _refreshing = False


def get_entries(source: str = "", q: str = "") -> list[dict]:
    with _lock:
        entries = list(_entries)
    if source in ("tool", "skill"):
        entries = [e for e in entries if e["source"] == source]
    if q:
        kw = q.lower()
        entries = [
            e for e in entries
            if kw in e["name"].lower()
            or kw in (e.get("description") or "").lower()
            or kw in (e.get("category") or "").lower()
            or any(kw in str(t).lower() for t in e.get("tags", []))
            or kw in (e.get("author") or "").lower()
        ]
    return entries


def stats() -> dict:
    with _lock:
        return {
            "count": len(_entries),
            "loaded_at": _loaded_at,
            "error": _last_error,
            "configured": is_configured(),
        }


def trigger_refresh() -> None:
    """后台触发一次刷新（webhook / 本站提交后调用）。

    若已有刷新在进行中，等它结束后再跑一次，保证触发不丢失。
    """
    def run():
        while True:
            with _lock:
                if not _refreshing:
                    break
            time.sleep(0.2)
        refresh()

    threading.Thread(target=run, daemon=True, name="library-refresh").start()
