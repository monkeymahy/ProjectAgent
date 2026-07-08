"""获取源码到本地目录：clone URL、解压 zip、或直接用本地路径。

所有操作都跑在受限临时目录里，有大小/超时限制。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from app.core.config import settings


class FetchError(Exception):
    pass


def _check_size(path: Path) -> None:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
            if total > settings.max_repo_size_mb * 1024 * 1024:
                raise FetchError(
                    f"仓库超过大小上限 {settings.max_repo_size_mb}MB"
                )


def _remove_git_and_ignored(repo_dir: Path) -> None:
    """删除 .git 目录、常见构建产物，减小解析体积。"""
    drop = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next"}
    for root, dirs, _files in os.walk(repo_dir, topdown=True):
        dirs[:] = [d for d in dirs if d not in drop]


def fetch_url(url: str, dest: Path) -> Path:
    """clone 一个 git URL 到 dest，depth 限制。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "git", "clone",
        "--depth", str(settings.clone_depth),
        "--single-branch",
        url, str(dest),
    ]
    try:
        subprocess.run(
            cmd,
            check=True,
            timeout=settings.clone_timeout,
            capture_output=True,
        )
    except FileNotFoundError as e:
        raise FetchError("未找到 git 命令，请安装 git") from e
    except subprocess.TimeoutExpired as e:
        raise FetchError(f"clone 超时（{settings.clone_timeout}s）") from e
    except subprocess.CalledProcessError as e:
        raise FetchError(
            f"clone 失败: {(e.stderr or b'').decode('utf-8', 'ignore')[:300]}"
        ) from e

    _remove_git_and_ignored(dest)
    _check_size(dest)
    return dest


def fetch_zip(zip_path: Path, dest: Path) -> Path:
    """解压用户上传的 zip 到 dest。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # zip slip 防护
            for name in zf.namelist():
                target = (dest / name).resolve()
                if not str(target).startswith(str(dest.resolve())):
                    raise FetchError("压缩包包含非法路径（zip slip）")
            zf.extractall(dest)
    except zipfile.BadZipFile as e:
        raise FetchError("压缩包损坏或非 zip 格式") from e

    # 如果解压出来只有一个顶层目录，下钻一层
    entries = [p for p in dest.iterdir() if not p.name.startswith(".")]
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for item in inner.iterdir():
            shutil.move(str(item), str(dest / item.name))
        inner.rmdir()

    _remove_git_and_ignored(dest)
    _check_size(dest)
    return dest


def fetch_local(local_path: str, dest: Path) -> Path:
    """复制一个本地目录到 dest（用于测试 / 本地项目提交）。"""
    src = Path(local_path).resolve()
    if not src.exists():
        raise FetchError(f"本地路径不存在: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        src,
        dest,
        ignore=shutil.ignore_patterns(
            ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"
        ),
    )
    _check_size(dest)
    return dest
