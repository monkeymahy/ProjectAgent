"""LLM 客户端：调用 OpenAI 兼容接口，解析 JSON 输出。

如果 config.yml 里没填 API key，会降级用一个"回退生成器"——
直接用解析出的元数据拼一份占位内容，保证链路可跑通（方便本地测试）。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

from app.core.config import settings
from app.llm.schema import build_prompt, build_incremental_prompt

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


def _extract_json(text: str) -> dict:
    """从 LLM 输出里抠出 JSON（可能被 ```json 包裹），并做容错修复。"""
    text = text.strip()
    if text.startswith("```"):
        # 去掉首尾 ``` 围栏
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 容错 1：去掉对象/数组里多余的尾逗号（},] 或 ],] 前）
    fixed = re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    # 容错 2：从文本里抠出第一个 {...} 块再试
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            fixed = re.sub(r",(\s*[}\]])", r"\1", m.group(0))
            return json.loads(fixed)
    raise LLMError(f"无法解析 LLM 输出为 JSON: {text[:200]}")


def _http_chat(prompt: str) -> str:
    """发一次 chat 请求，返回 content 文本。失败抛 LLMError。"""
    cfg = settings.llm
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": "你是项目展示页生成助手，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
    }
    log.info("LLM 请求: model=%s prompt_len=%d", cfg.model, len(prompt))
    start = time.monotonic()
    try:
        with httpx.Client(timeout=cfg.timeout) as client:
            resp = client.post(
                f"{cfg.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        log.error("LLM HTTP 错误: %s %s", e.response.status_code, e.response.text[:200])
        raise LLMError(f"LLM HTTP 错误: {e.response.status_code} {e.response.text[:200]}") from e
    except Exception as e:
        log.error("LLM 调用失败: %s", e)
        raise LLMError(f"LLM 调用失败: {e}") from e
    elapsed = time.monotonic() - start
    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage") or {}
    log.info(
        "LLM 响应: 耗时=%.1fs content_len=%d token=prompt/%s completion/%s total/%s",
        elapsed,
        len(content),
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
        usage.get("total_tokens"),
    )
    return content


def call_llm(prompt: str) -> dict[str, Any]:
    cfg = settings.llm
    if not cfg.api_key:
        log.warning("LLM_API_KEY 未配置，使用回退生成器")
        raise LLMError("LLM_API_KEY 未配置")

    content = _http_chat(prompt)
    try:
        return _extract_json(content)
    except LLMError as e:
        # 第一次解析失败，重试一次并提醒格式
        log.warning("首次 JSON 解析失败，重试：%s", e)
        retry_prompt = (
            "你上次的输出不是合法 JSON，请严格输出合法 JSON（无注释、无尾逗号、"
            "字符串用双引号），不要任何额外文字。原始任务如下：\n\n" + prompt
        )
        content = _http_chat(retry_prompt)
        return _extract_json(content)


def fallback_generate(parsed: dict) -> dict[str, Any]:
    """未配置 LLM 时的回退：直接从元数据拼一份占位展示内容。"""
    name = parsed.get("name", "未命名项目")
    langs = list(parsed.get("languages", {}).keys())
    tech = parsed.get("tech_stack", [])
    return {
        "title": name,
        "one_line_summary": parsed.get("description") or f"一个 {', '.join(langs[:3]) or '未知'} 项目",
        "highlights": [
            f"主要语言：{', '.join(langs[:5]) or '未识别'}",
            f"技术栈包含：{', '.join(tech[:6]) or '未识别'}",
            f"License：{parsed.get('license', '未声明')}",
            f"入口文件：{', '.join(parsed.get('entry_hints', [])) or '未识别'}",
        ][:5],
        "tech_stack": tech or langs,
        "architecture_overview": (
            parsed.get("description")
            or "（架构概述需配置 LLM 后由模型基于 README 生成）"
        ),
        "use_cases": ["详见 README"],
        "getting_started": "（快速上手说明需配置 LLM 后生成，或参考项目 README）",
        "tags": langs[:5] or ["misc"],
    }


def generate(parsed: dict) -> dict[str, Any]:
    """对外入口：优先调 LLM，失败/未配置则回退。"""
    try:
        result = call_llm(build_prompt(parsed))
        log.info("LLM 生成成功: title=%s", (result.get("title") or "")[:40])
        return result
    except LLMError as e:
        log.warning("LLM 生成失败，使用回退：%s", e)
        return fallback_generate(parsed)


def generate_incremental(
    parsed: dict, old_generated: dict, changed_files: list[str], affected_fields: list[str],
) -> dict:
    """L2 增量：只更新受影响字段。失败/未配置返回 {}，调用方保持旧值。"""
    if not settings.llm.api_key:
        log.warning("L2 增量需要 LLM，未配置 API key")
        return {}
    try:
        result = call_llm(build_incremental_prompt(parsed, old_generated, changed_files, affected_fields))
        log.info("L2 增量成功: 更新字段=%s", list(result.keys()))
        return result
    except LLMError as e:
        log.warning("L2 增量 LLM 失败，保持旧值: %s", e)
        return {}
