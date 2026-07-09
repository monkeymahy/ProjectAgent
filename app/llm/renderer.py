"""渲染器：把 LLM 生成的 JSON + 解析元数据 套进 Jinja2 模板，产出 HTML。"""
from __future__ import annotations

from pathlib import Path

import bleach
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.core.config import settings

TEMPLATE_VERSION = 1
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_template(name: str, **context) -> str:
    """渲染任意模板（供 API 层渲染列表页等使用）。"""
    return _env.get_template(name).render(**context)


def _build_tree_text(tree: list[str]) -> str:
    """把扁平路径列表渲染成缩进树形文本。"""
    root: dict = {}
    for path in tree:
        parts = path.split("/")
        node = root
        for part in parts:
            node = node.setdefault(part, {})
    lines: list[str] = []

    def render(node: dict, prefix: str) -> None:
        items = sorted(node.items(), key=lambda kv: (kv[1] == {}, kv[0]))
        for i, (name, children) in enumerate(items):
            is_last = i == len(items) - 1
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{name}")
            if children:
                extension = "    " if is_last else "│   "
                render(children, prefix + extension)

    render(root, "")
    return "\n".join(lines)


def _sanitize_text_fields(gen: dict) -> dict:
    """对 LLM 生成的纯文本字段做转义防注入。先剥 HTML 标签，再 bleach 清洗。"""
    import re
    safe = bleach.clean

    def clean_str(s: str) -> str:
        s = re.sub(r"<[^>]+>", "", s)
        return safe(s, tags=[], strip=True)

    out = dict(gen)
    for k in ("title", "one_line_summary", "architecture_overview", "getting_started"):
        if k in out and isinstance(out[k], str):
            out[k] = clean_str(out[k])
    for k in ("highlights", "use_cases", "tags", "tech_stack"):
        if k in out and isinstance(out[k], list):
            out[k] = [clean_str(str(x)) for x in out[k]]
    return out


def render_page(
    parsed: dict,
    generated: dict,
    project_id: str = "",
    source: str = "",
    source_type: str = "",
) -> str:
    gen = _sanitize_text_fields(generated)
    template = _env.get_template("project_page.html")
    html = template.render(
        title=gen.get("title", parsed.get("name", "项目")),
        one_line_summary=gen.get("one_line_summary", ""),
        highlights=gen.get("highlights", []),
        tech_stack=gen.get("tech_stack", []) or list(parsed.get("languages", {}).keys()),
        architecture_overview=gen.get("architecture_overview", ""),
        use_cases=gen.get("use_cases", []),
        getting_started=gen.get("getting_started", ""),
        tags=gen.get("tags", []),
        # 元数据
        license=parsed.get("license"),
        entry_hints=parsed.get("entry_hints", []),
        languages=parsed.get("languages", {}),
        dependencies=parsed.get("dependencies", {}),
        tree=parsed.get("tree", []),
        tree_text=_build_tree_text(parsed.get("tree", [])),
        readme_html=parsed.get("readme_html", ""),
        # 源码链接
        project_id=project_id,
        source=source or "",
        source_type=source_type or "",
        template_version=TEMPLATE_VERSION,
    )
    return html
