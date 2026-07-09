"""Celery 任务：parse → generate → render，全程更新状态。"""
from __future__ import annotations

import logging
from pathlib import Path

from app.core.celery_app import celery_app
from app.core.config import settings
from app.models.models import (
    TaskStatus, update_status, set_failed, get_project, upsert_card,
)
from app.sandbox.fetcher import fetch_url, fetch_zip, fetch_local, FetchError
from app.parsers.analyzer import analyze
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

    source_type = project["source_type"]
    source = project["source"]

    try:
        # 1. 获取源码
        update_status(project_id, TaskStatus.CLONING, 10, "正在获取项目源码...")
        repo_dir = settings.repos_dir / project_id
        if source_type == "url":
            fetch_url(source, repo_dir)
        elif source_type == "zip":
            fetch_zip(Path(source), repo_dir)
        elif source_type == "local":
            fetch_local(source, repo_dir)
        else:
            raise FetchError(f"未知来源类型: {source_type}")

        # 2. 解析
        update_status(project_id, TaskStatus.PARSING, 35, "正在解析项目结构...")
        parsed = analyze(repo_dir)

        # 3. LLM 生成
        update_status(project_id, TaskStatus.GENERATING, 60, "正在生成展示内容...")
        generated = generate(parsed)

        # 4. 渲染
        update_status(project_id, TaskStatus.GENERATING, 85, "正在渲染展示页...")
        html = render_page(
            parsed, generated,
            project_id=project_id, source=source, source_type=source_type,
        )

        # 5. 落盘
        page_path = settings.pages_dir / f"{project_id}.html"
        page_path.write_text(html, encoding="utf-8")

        # 6. 写入社区卡片摘要表
        upsert_card(
            project_id,
            parsed,
            generated,
            owner_name=project.get("owner_name", "匿名"),
            owner_id=project.get("owner_id"),
        )

        update_status(
            project_id,
            TaskStatus.DONE,
            100,
            "完成",
            parsed_json=parsed,
            generated_json=generated,
            html_path=str(page_path),
        )
        log.info("项目 %s 生成完成: %s", project_id, page_path)

    except FetchError as e:
        log.error("获取源码失败: %s", e)
        set_failed(project_id, f"获取源码失败: {e}")
    except Exception as e:
        log.exception("处理项目 %s 失败", project_id)
        set_failed(project_id, f"处理失败: {e}")
