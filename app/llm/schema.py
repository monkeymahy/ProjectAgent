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

【代码结构】（tree-sitter 从源码提取的真实符号，据此描述架构，勿编造）
{code_structure}

【上下文提示】
{context_hints}

现在输出 JSON："""


def _build_context_hints(meta: dict) -> str:
    """按项目元数据的特征生成动态提示，引导 LLM 在信息不足时留空而非瞎编。"""
    hints: list[str] = []
    if not meta.get("readme"):
        hints.append("- 项目未提供 README，请主要依据目录结构、依赖与代码线索推断功能，不确定处留空。")
    if not meta.get("dependencies"):
        if meta.get("tech_stack"):
            hints.append("- 未发现依赖清单文件，技术栈基于源码 import 语句推断，可能不全。")
        else:
            hints.append("- 未发现依赖清单，技术栈信息不足，对应字段留空。")
    if meta.get("entry_hints"):
        hints.append(
            f"- 已识别入口文件 {', '.join(meta['entry_hints'])}，"
            f"getting_started 可据此给出安装运行步骤。"
        )
    else:
        hints.append("- 未识别明确入口文件，getting_started 给出通用指引即可，不要编造具体命令。")
    if not meta.get("license") or meta.get("license") == "未声明":
        hints.append("- License 未声明，不要猜测协议类型。")
    return "\n".join(hints) if hints else "无额外提示。"


def _build_code_structure_text(code_structure: dict) -> str:
    """把代码结构摘要格式化成文本，控制总长度。"""
    if not code_structure:
        return "（未提取到代码结构，可能语言暂不支持或无源码）"
    lines: list[str] = []
    total = 0
    for rel, symbols in list(code_structure.items())[:30]:
        line = f"{rel}: {', '.join(symbols[:15])}"
        lines.append(line)
        total += len(line)
        if total > 2500:
            lines.append("...（更多文件已省略）")
            break
    return "\n".join(lines)


def _build_meta(parsed_metadata: dict) -> dict:
    """精简 metadata，控制 token：readme 取前一部分，tree 取前 80 项。"""
    return {
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


def build_prompt(parsed_metadata: dict) -> str:
    import json
    meta = _build_meta(parsed_metadata)
    return PROMPT_TEMPLATE.format(
        schema=json.dumps(OUTPUT_SCHEMA, ensure_ascii=False, indent=2),
        metadata=json.dumps(meta, ensure_ascii=False, indent=2),
        code_structure=_build_code_structure_text(parsed_metadata.get("code_structure") or {}),
        context_hints=_build_context_hints(meta),
    )


INCREMENTAL_TEMPLATE = """项目源码有部分更新，请基于最新信息只更新受影响的字段。

【旧的项目展示内容】
{old_generated}

【变化的文件】
{changed_files}

【需要更新的字段】（只输出这些字段，其他不要输出）
{affected_fields}

【最新项目元数据】
{metadata}

【最新代码结构】
{code_structure}

【硬性要求】
1. 只输出"需要更新的字段"中列出的字段，其他字段不要输出。
2. 基于最新元数据和代码结构更新这些字段，不要编造不存在的功能。
3. 输出纯 JSON，无 markdown 围栏，无解释文字。
现在输出 JSON："""


def build_incremental_prompt(
    parsed: dict, old_generated: dict, changed_files: list[str], affected_fields: list[str],
) -> str:
    import json
    return INCREMENTAL_TEMPLATE.format(
        old_generated=json.dumps(old_generated, ensure_ascii=False, indent=2),
        changed_files="\n".join(changed_files),
        affected_fields=", ".join(affected_fields),
        metadata=json.dumps(_build_meta(parsed), ensure_ascii=False, indent=2),
        code_structure=_build_code_structure_text(parsed.get("code_structure") or {}),
    )
