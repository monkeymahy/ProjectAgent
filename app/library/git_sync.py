"""git 只读克隆同步：clone / pull 外部工具库、技能库仓库。"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from app.core.config import settings

log = logging.getLogger(__name__)


def _inject_auth(url: str, user: str = "", token: str = "") -> str:
    """把 user:token 注入 HTTPS URL；本地路径或 URL 已带认证则原样返回。"""
    if not user or not token:
        return url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or "@" in parts.netloc:
        return url
    userinfo = f"{quote(user, safe='')}:{quote(token, safe='')}"
    netloc = f"{userinfo}@{parts.netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _run(args: list[str], cwd: Path | None = None) -> str:
    # 凭证可能出现在 URL 参数里,不落日志
    proc = subprocess.run(
        args, cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[1]} 失败: {proc.stderr.strip()[:300]}")
    return proc.stdout


def ensure_repo(
    name: str, url: str, branch: str = "",
    user: str = "", token: str = "",
) -> Path:
    """仓库不存在则 clone，存在则更新到远端最新。返回本地路径。

    认证注入到 origin 远程地址里,后续 fetch/pull 复用。
    """
    auth_url = _inject_auth(url, user, token)
    dest = settings.library_repos_dir / name
    if not (dest / ".git").exists():
        settings.library_repos_dir.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            # 残留的不完整目录，重新 clone 前清掉
            import shutil
            shutil.rmtree(dest)
        args = ["git", "clone"]
        if branch:
            args += ["-b", branch]
        args += [auth_url, str(dest)]
        _run(args)
        log.info("已克隆库仓库 %s -> %s", _inject_auth(url), dest)
        return dest

    _run(["git", "fetch", "--all"], cwd=dest)
    cur = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=dest).strip()
    try:
        _run(["git", "pull", "--ff-only"], cwd=dest)
    except RuntimeError:
        # 远端被 force-push 等：硬重置到远端分支，以对方仓库为准
        _run(["git", "reset", "--hard", f"origin/{cur}"], cwd=dest)
        log.warning("库仓库 %s 非快进更新，已重置到 origin/%s", name, cur)
    return dest
