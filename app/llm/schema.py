"""LLM 生成展示页内容的 JSON schema 与 prompt 模板。

LLM 只产出结构化 JSON，不碰 HTML。后端用 Jinja2 套模板渲染，
bleach 清洗。这样避免幻觉造标签 / XSS / 样式崩。
"""
from __future__ import annotations

# 期望 LLM 输出的 JSON schema
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "项目展示标题"},
        "one_line_summary": {"type": "string", "description": "一句话概括项目是做什么的"},
        "highlights": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-5 条核心亮点",
        },
        "tech_stack": {
            "type": "array",
            "items": {"type": "string"},
            "description": "技术栈清单",
        },
        "architecture_overview": {
            "type": "string",
            "description": "项目架构/实现思路概述，2-4 段",
        },
        "use_cases": {
            "type": "array",
            "items": {"type": "string"},
            "description": "适用场景",
        },
        "getting_started": {
            "type": "string",
            "description": "快速上手说明（如何安装运行）",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-6 个分类标签，便于社区检索",
        },
    },
    "required": [
        "title", "one_line_summary", "highlights", "tech_stack",
        "architecture_overview", "use_cases", "getting_started", "tags",
    ],
}


PROMPT_TEMPLATE = """你是一个技术社区的项目展示页生成助手。我会给你一个软件项目的解析元数据（README、目录结构、语言、依赖等）。
你的任务：基于这些**真实**信息，生成项目展示页的结构化内容。

【硬性要求】
1. 只根据我提供的元数据生成，绝不编造项目中不存在的功能、文件或能力。
2. 如果信息不足，对应字段留空或写"信息不足"，不要瞎编。
3. 输出**纯 JSON**，不要 markdown 代码块包裹，不要任何解释文字。
4. JSON 必须符合下面这个 schema：
{schema}

【项目元数据】
{metadata}

现在输出 JSON："""


def build_prompt(parsed_metadata: dict) -> str:
    import json
    # 精简 metadata，控制 token：readme 取前一部分，tree 取前 80 项
    meta = {
        "name": parsed_metadata.get("name"),
        "description": parsed_metadata.get("description"),
        "languages": parsed_metadata.get("languages"),
        "tech_stack": parsed_metadata.get("tech_stack"),
        "dependencies": parsed_metadata.get("dependencies"),
        "license": parsed_metadata.get("license"),
        "entry_hints": parsed_metadata.get("entry_hints"),
        "tree": parsed_metadata.get("tree", [])[:80],
        "readme": (parsed_metadata.get("readme_raw") or "")[:4000],
    }
    return PROMPT_TEMPLATE.format(
        schema=json.dumps(OUTPUT_SCHEMA, ensure_ascii=False, indent=2),
        metadata=json.dumps(meta, ensure_ascii=False, indent=2),
    )
