"""git 只读克隆同步：clone / pull 外部工具库、技能库仓库。"""
from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from app.core.config import settings

log = logging.getLogger(__name__)

# 认证失败类错误的关键词，命中时附加配置提示
_AUTH_ERRS = ("authentication", "terminal prompts disabled", "could not read username",
              "403", "permission denied", "fatal: unable to access")


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


def _run(args: list[str], cwd: Path | None = None, label: str = "") -> str:
    # 凭证可能出现在 URL 参数里,不落日志
    # 禁用一切交互式凭据提示(终端/凭据管理器弹窗),缺凭据立即失败而不是挂起刷新线程
    env = {**os.environ,
           "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": "credential.helper",
           "GIT_CONFIG_VALUE_0": ""}
    proc = subprocess.run(
        args, cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600, env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {label or args[1]} 失败: {proc.stderr.strip()[:300]}")
    return proc.stdout


def _git(args: list[str], cwd: Path | None = None) -> str:
    return _run(["git", "-c", "credential.helper=", *args], cwd=cwd, label=args[0])


def last_commit_times(repo_dir: Path) -> dict[str, str]:
    """一次 git log 拿到每个文件的最后提交时间（ISO 8601，UTC）。

    返回 {相对路径(正斜杠): 提交时间}。git log 从新到旧输出，文件首次出现即最新提交。
    """
    out = _git(["log", "--format=@@@%cI", "--name-only", "--no-renames"], cwd=repo_dir)
    times: dict[str, str] = {}
    cur = ""
    for line in out.splitlines():
        if line.startswith("@@@"):
            cur = line[3:].strip()
        elif line.strip():
            p = line.strip().replace("\\", "/")
            if p not in times:
                times[p] = cur
    return times


def ensure_repo(
    name: str, url: str, branch: str = "",
    user: str = "", token: str = "",
) -> Path:
    """仓库不存在则 clone，存在则更新到远端最新。返回本地路径。

    认证注入到 origin 远程地址里,后续 fetch/pull 复用;
    每次刷新都会重写 origin 地址,保证配置里补了凭据后无需重新克隆。
    """
    auth_url = _inject_auth(url, user, token)
    dest = settings.library_repos_dir / name
    try:
        if not (dest / ".git").exists():
            settings.library_repos_dir.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                # 残留的不完整目录，重新 clone 前清掉
                import shutil
                shutil.rmtree(dest)
            args = ["clone"]
            if branch:
                args += ["-b", branch]
            _git([*args, auth_url, str(dest)])
            log.info("已克隆库仓库 %s -> %s", _inject_auth(url), dest)
            return dest

        # origin 地址可能不带凭据(旧克隆/配置后补的凭据),重写保证 fetch 不再要密码
        _git(["remote", "set-url", "origin", auth_url], cwd=dest)
        _git(["fetch", "--all"], cwd=dest)
        cur = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=dest).strip()
        try:
            _git(["pull", "--ff-only"], cwd=dest)
        except RuntimeError:
            # 远端被 force-push 等：硬重置到远端分支，以对方仓库为准
            _git(["reset", "--hard", f"origin/{cur}"], cwd=dest)
            log.warning("库仓库 %s 非快进更新，已重置到 origin/%s", name, cur)
        return dest
    except RuntimeError as e:
        msg = str(e)
        low = msg.lower()
        if any(k in low for k in _AUTH_ERRS):
            msg += "（仓库需要认证：请在 config.yml 填写 LIBRARY_TOOL/SKILL_REPO_USER 与 TOKEN）"
        raise RuntimeError(msg)
