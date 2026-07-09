"""Celery 任务：parse → generate → render，全程更新状态。"""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from app.core.celery_app import celery_app
from app.core.config import settings
from app.models.models import (
    TaskStatus, update_status, set_failed, get_project, upsert_card,
    get_file_hashes, save_file_hashes,
)
from app.sandbox.fetcher import fetch_url, fetch_zip, fetch_local, FetchError
from app.parsers.analyzer import (
    analyze, compute_source_hash, compute_file_hashes, is_non_critical,
)
from app.llm.client import generate
from app.llm.renderer import render_page

log = logging.getLogger(__name__)


@celery_app.task(name="process_project", bind=True)
def process_project(self, project_id: str) -> None:
    settings.ensure_dirs()
    project = get_project(project_id)
    if not project:
        log.error("项目 %s 不存在", project_id)
        return
    _run_pipeline(project_id, project)


@celery_app.task(name="sync_project", bind=True)
def sync_project(self, project_id: str) -> None:
    """同步更新：重新拉取源码，源码无变化则跳过 LLM（L0），否则全量重生。"""
    settings.ensure_dirs()
    project = get_project(project_id)
    if not project:
        log.error("项目 %s 不存在", project_id)
        return
    _run_pipeline(project_id, project)


def _fetch_source(source_type: str, source: str, repo_dir: Path) -> None:
    if source_type == "url":
        fetch_url(source, repo_dir)
    elif source_type == "zip":
        fetch_zip(Path(source), repo_dir)
    elif source_type == "local":
        fetch_local(source, repo_dir)
    else:
        raise FetchError(f"未知来源类型: {source_type}")


def _atomic_write_html(page_path: Path, html: str) -> None:
    """先写临时文件再原子替换，避免生成中途崩溃留下半截 HTML。"""
    tmp = page_path.with_suffix(page_path.suffix + ".tmp")
    tmp.write_text(html, encoding="utf-8")
    os.replace(tmp, page_path)


def _diff_file_hashes(old: dict[str, str], new: dict[str, str]) -> list[str]:
    """返回变化的文件路径（新增/修改/删除），排序。"""
    changed: set[str] = set()
    for rel, h in new.items():
        if old.get(rel) != h:
            changed.add(rel)
    for rel in old:
        if rel not in new:
            changed.add(rel)
    return sorted(changed)


def _run_pipeline(project_id: str, project: dict) -> None:
    """fetch -> analyze -> generate -> render 流水线，首次生成与同步更新共用。

    同步更新时比对 source_hash：源码无变化则跳过 LLM（L0）。
    """
    source_type = project["source_type"]
    source = project["source"]
    repo_dir = settings.repos_dir / project_id

    try:
        # 1. 重新获取源码（覆盖旧目录）
        update_status(project_id, TaskStatus.CLONING, 10, "正在获取项目源码...")
        log.info("项目 %s 获取源码: type=%s source=%s", project_id, source_type, source)
        if repo_dir.exists():
            shutil.rmtree(repo_dir)
        _fetch_source(source_type, source, repo_dir)

        # 2. 整体指纹比对：无变化跳过 LLM（L0）
        new_hash = compute_source_hash(repo_dir)
        old_hash = project.get("source_hash")
        if old_hash and new_hash == old_hash:
            update_status(project_id, TaskStatus.DONE, 100, "源码无变化，已是最新")
            log.info("项目 %s 源码无变化，跳过生成 (hash=%s)", project_id, new_hash[:12])
            return

        # 3. 文件级变化检测：仅非关键文件变化则跳过 LLM（L1），复用旧 generated_json
        new_file_hashes = compute_file_hashes(repo_dir)
        old_file_hashes = get_file_hashes(project_id)
        changed = _diff_file_hashes(old_file_hashes, new_file_hashes)
        if old_hash and project.get("generated_json") and changed and all(
            is_non_critical(rel) for rel in changed
        ):
            log.info(
                "项目 %s L1 跳过 LLM: 仅非关键文件变化 %d 个 %s",
                project_id, len(changed), changed[:5],
            )
            update_status(project_id, TaskStatus.PARSING, 35, "正在解析项目结构...")
            parsed = analyze(repo_dir)
            raw_gen = project["generated_json"]
            generated = json.loads(raw_gen) if isinstance(raw_gen, str) else raw_gen
            update_status(project_id, TaskStatus.GENERATING, 85, "正在渲染展示页...")
            html = render_page(
                parsed, generated,
                project_id=project_id, source=source, source_type=source_type,
            )
            page_path = settings.pages_dir / f"{project_id}.html"
            _atomic_write_html(page_path, html)
            upsert_card(
                project_id, parsed, generated,
                owner_name=project.get("owner_name", "匿名"),
                owner_id=project.get("owner_id"),
            )
            save_file_hashes(project_id, new_file_hashes)
            update_status(
                project_id, TaskStatus.DONE, 100, "非关键文件变化，已跳过 LLM 更新",
                parsed_json=parsed, generated_json=generated,
                html_path=str(page_path), source_hash=new_hash,
            )
            log.info("项目 %s L1 更新完成（跳过 LLM）", project_id)
            return

        if old_hash:
            log.info("项目 %s 源码有变化: old=%s new=%s，全量重生", project_id, old_hash[:12], new_hash[:12])
        else:
            log.info("项目 %s 首次生成 (hash=%s)", project_id, new_hash[:12])

        # 4. 解析
        update_status(project_id, TaskStatus.PARSING, 35, "正在解析项目结构...")
        parsed = analyze(repo_dir)
        log.info(
            "项目 %s 解析完成: 语言=%s 文件数=%d",
            project_id,
            list((parsed.get("languages") or {}).keys()),
            len(parsed.get("tree") or []),
        )

        # 5. LLM 生成
        update_status(project_id, TaskStatus.GENERATING, 60, "正在生成展示内容...")
        generated = generate(parsed)

        # 6. 渲染 + 原子落盘
        update_status(project_id, TaskStatus.GENERATING, 85, "正在渲染展示页...")
        html = render_page(
            parsed, generated,
            project_id=project_id, source=source, source_type=source_type,
        )
        page_path = settings.pages_dir / f"{project_id}.html"
        _atomic_write_html(page_path, html)

        # 7. 写入社区卡片摘要表 + 文件级 hash
        upsert_card(
            project_id, parsed, generated,
            owner_name=project.get("owner_name", "匿名"),
            owner_id=project.get("owner_id"),
        )
        save_file_hashes(project_id, new_file_hashes)

        update_status(
            project_id, TaskStatus.DONE, 100, "完成",
            parsed_json=parsed, generated_json=generated,
            html_path=str(page_path), source_hash=new_hash,
        )
        log.info("项目 %s 生成完成: %s", project_id, page_path)

    except FetchError as e:
        log.error("获取源码失败: %s", e)
        set_failed(project_id, f"获取源码失败: {e}")
    except Exception as e:
        log.exception("处理项目 %s 失败", project_id)
        set_failed(project_id, f"处理失败: {e}")
