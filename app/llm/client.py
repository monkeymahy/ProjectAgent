"""LLM 客户端：调用 OpenAI 兼容接口，解析 JSON 输出。

如果 config.yml 里没填 API key，会降级用一个"回退生成器"——
直接用解析出的元数据拼一份占位内容，保证链路可跑通（方便本地测试）。
"""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx

from app.core.config import settings
from app.llm.schema import build_prompt, build_incremental_prompt, COMPACT_LIMITS, _build_code_structure_text

log = logging.getLogger(__name__)


# 分块摘要阈值:code_structure 原始文本超过此字符数时,分块调 LLM 生成摘要再合并
CODE_STRUCTURE_SUMMARIZE_THRESHOLD = 60000
CHUNK_CHARS = 30000
SUMMARIZE_MAX_WORKERS = 8

_SUMMARY_TEMPLATE = """以下是项目部分源码的 tree-sitter 符号清单。请用 2-4 句中文概括这批文件实现的功能与技术职责,基于符号名推断,不编造细节:

{block}"""


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


def _is_context_too_large(e: LLMError) -> bool:
    """判断 LLM 错误是否为上下文超限（不同 provider 文案不一，关键词匹配）。"""
    msg = str(e).lower()
    return any(k in msg for k in ("context", "length", "too long", "maximum", "token limit", "too many"))


def _summarize_chunk(block: dict, idx: int) -> str:
    """对单个分块调 LLM 生成摘要,失败降级为本地符号拼接。"""
    block_text = _build_code_structure_text(block)
    prompt = _SUMMARY_TEMPLATE.format(block=block_text)
    try:
        content = _http_chat(prompt).strip()
        log.info("分块摘要[%d] 完成: len=%d", idx, len(content))
        return content
    except LLMError as e:
        log.warning("分块摘要[%d] 失败,降级本地符号: %s", idx, e)
        return block_text


def summarize_code_structure_chunked(code_structure: dict) -> str:
    """把超大的 code_structure 分块,并发调 LLM 生成摘要,合并成一段文本。"""
    blocks: list[dict] = []
    cur: dict[str, list[str]] = {}
    cur_size = 0
    for rel, symbols in code_structure.items():
        line_len = len(rel) + 2 + sum(len(s) + 2 for s in symbols)
        if cur and cur_size + line_len > CHUNK_CHARS:
            blocks.append(cur)
            cur = {}
            cur_size = 0
        cur[rel] = symbols
        cur_size += line_len
    if cur:
        blocks.append(cur)

    n = len(blocks)
    log.info("分块摘要: 共 %d 块", n)
    start = time.monotonic()
    summaries: list[str] = [""] * n
    with ThreadPoolExecutor(max_workers=min(SUMMARIZE_MAX_WORKERS, n)) as pool:
        futures = {pool.submit(_summarize_chunk, b, i): i for i, b in enumerate(blocks)}
        for fut in as_completed(futures):
            summaries[futures[fut]] = fut.result()
    elapsed = time.monotonic() - start
    log.info("分块摘要完成: %d 块, 耗时=%.1fs", n, elapsed)
    return f"（分块摘要，共 {n} 块）\n\n" + "\n\n".join(summaries)


def generate(parsed: dict) -> dict[str, Any]:
    """对外入口:code_structure 超阈值时分块摘要压缩;上下文超限时 COMPACT 截断兜底;仍失败则回退。"""
    cs = parsed.get("code_structure") or {}
    cs_text_full = _build_code_structure_text(cs)
    override = None
    if len(cs_text_full) > CODE_STRUCTURE_SUMMARIZE_THRESHOLD:
        log.info("code_structure 超阈值(%d 字符),分块摘要压缩", len(cs_text_full))
        override = summarize_code_structure_chunked(cs)
    try:
        prompt = build_prompt(parsed, code_structure_override=override)
        result = call_llm(prompt)
        log.info("LLM 生成成功: title=%s", (result.get("title") or "")[:40])
        return result
    except LLMError as e:
        if _is_context_too_large(e):
            log.warning("prompt 超出上下文，压缩后重试: %s", e)
            try:
                result = call_llm(build_prompt(parsed, limits=COMPACT_LIMITS))
                log.info("LLM 压缩重试成功: title=%s", (result.get("title") or "")[:40])
                return result
            except LLMError as e2:
                log.warning("压缩重试仍失败，使用回退: %s", e2)
                return fallback_generate(parsed)
        log.warning("LLM 生成失败，使用回退: %s", e)
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
