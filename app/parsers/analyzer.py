"""从已获取到本地的项目目录解析出结构化元数据 JSON。

输出 schema 见 ParsedProject：
{
  name, description, readme_raw, readme_html,
  languages: {lang: file_count},
  tech_stack: [str],
  dependencies: {ecosystem: [pkg]},
  tree: [path, ...],
  license: str,
  entry_hints: [str]
}
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import markdown as md
import bleach

from app.core.config import settings

# 扩展名 -> 语言（精简版，类似 Linguist）
EXT_LANG = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".jsx": "JSX",
    ".tsx": "TSX", ".java": "Java", ".kt": "Kotlin", ".go": "Go", ".rs": "Rust",
    ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".hpp": "C++",
    ".cs": "C#", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift",
    ".m": "Objective-C", ".scala": "Scala", ".clj": "Clojure",
    ".html": "HTML", ".css": "CSS", ".scss": "SCSS", ".vue": "Vue",
    ".svelte": "Svelte",
    ".sh": "Shell", ".bash": "Shell", ".ps1": "PowerShell",
    ".sql": "SQL", ".r": "R", ".m": "MATLAB", ".jl": "Julia",
    ".dart": "Dart", ".lua": "Lua", ".pl": "Perl",
    ".yml": "YAML", ".yaml": "YAML", ".json": "JSON", ".toml": "TOML",
    ".xml": "XML", ".md": "Markdown", ".rst": "reStructuredText",
    ".ipynb": "Jupyter Notebook",
}

# 依赖清单文件：文件名 -> (生态系统, 提取函数名)
DEP_FILES = {
    "package.json": "node",
    "requirements.txt": "python",
    "Pipfile": "python",
    "pyproject.toml": "python",
    "go.mod": "go",
    "Cargo.toml": "rust",
    "pom.xml": "maven",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    "Gemfile": "ruby",
    "composer.json": "php",
}

# 技术栈关键词映射（从依赖/文件名推断）
TECH_KEYWORDS = {
    "fastapi": "FastAPI", "flask": "Flask", "django": "Django",
    "celery": "Celery", "redis": "Redis", "numpy": "NumPy",
    "pandas": "Pandas", "torch": "PyTorch", "tensorflow": "TensorFlow",
    "opencv": "OpenCV", "scikit-learn": "scikit-learn",
    "react": "React", "vue": "Vue", "next": "Next.js", "vite": "Vite",
    "express": "Express", "nestjs": "NestJS",
    "pyqt": "PyQt", "pyside": "PySide",
    "pythonocc": "pythonOCC", "opencascade": "OpenCASCADE",
}

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", "target", ".idea", ".vscode",
    "site-packages", ".mypy_cache", ".pytest_cache",
}


def _find_readme(repo_dir: Path) -> Optional[Path]:
    for name in ("README.md", "README.rst", "README.txt", "README", "readme.md"):
        p = repo_dir / name
        if p.is_file():
            return p
    for p in repo_dir.iterdir():
        if p.is_file() and p.name.lower().startswith("readme"):
            return p
    return None


def _find_license(repo_dir: Path) -> Optional[str]:
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE", "COPYING"):
        p = repo_dir / name
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="ignore")
            if "MIT" in text[:200]:
                return "MIT"
            if "Apache License" in text[:400]:
                return "Apache-2.0"
            if "BSD" in text[:200]:
                return "BSD"
            if "GNU GENERAL PUBLIC" in text[:400]:
                return "GPL"
            return name
    return None


def _build_tree(repo_dir: Path) -> list[str]:
    entries: list[str] = []
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS and not d.startswith("."))
        dirs.sort()
        for f in sorted(files):
            if f.startswith(".") and f not in (".gitignore", ".env.example"):
                continue
            full = Path(root) / f
            rel = full.relative_to(repo_dir).as_posix()
            entries.append(rel)
            if len(entries) >= settings.max_tree_entries:
                return entries
    return entries


def _count_languages(repo_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]
        for f in files:
            ext = Path(f).suffix.lower()
            if ext in EXT_LANG:
                counts[EXT_LANG[ext]] = counts.get(EXT_LANG[ext], 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _parse_deps(repo_dir: Path) -> dict[str, list[str]]:
    deps: dict[str, list[str]] = {}
    for name, ecosystem in DEP_FILES.items():
        p = repo_dir / name
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        pkgs = _extract_deps(name, text)
        if pkgs:
            deps[ecosystem] = pkgs[:50]
    return deps


def _extract_deps(filename: str, text: str) -> list[str]:
    pkgs: list[str] = []
    if filename == "package.json" or filename == "composer.json":
        try:
            import json
            data = json.loads(text)
            for section in ("dependencies", "devDependencies", "require"):
                if isinstance(data.get(section), dict):
                    pkgs.extend(data[section].keys())
        except Exception:
            pass
    elif filename == "requirements.txt":
        for line in text.splitlines():
            line = line.strip().split("#")[0].strip()
            if not line:
                continue
            m = re.match(r"^([A-Za-z0-9_\-.]+)", line)
            if m:
                pkgs.append(m.group(1).lower())
    elif filename == "go.mod":
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("require "):
                parts = line.split()
                if len(parts) >= 2:
                    pkgs.append(parts[1])
    elif filename == "Cargo.toml":
        in_deps = False
        for line in text.splitlines():
            if line.strip().startswith("[dependencies]"):
                in_deps = True
                continue
            if line.strip().startswith("["):
                in_deps = False
                continue
            if in_deps:
                m = re.match(r"^([A-Za-z0-9_\-.]+)\s*=", line)
                if m:
                    pkgs.append(m.group(1))
    elif filename in ("pom.xml", "build.gradle", "build.gradle.kts"):
        for m in re.finditer(r"<artifactId>([^<]+)</artifactId>", text):
            pkgs.append(m.group(1))
        for m in re.finditer(r"implementation\s+['\"]([^'\":\s]+)", text):
            pkgs.append(m.group(1))
    elif filename == "Gemfile":
        for line in text.splitlines():
            m = re.match(r"^\s*gem\s+['\"]([^'\"]+)", line)
            if m:
                pkgs.append(m.group(1))
    elif filename == "Pipfile" or filename == "pyproject.toml":
        for line in text.splitlines():
            m = re.match(r"^([A-Za-z0-9_\-.]+)\s*=", line)
            if m and not line.startswith("["):
                pkgs.append(m.group(1).lower())
    return pkgs


def _infer_tech_stack(languages: dict, deps: dict, repo_dir: Path) -> list[str]:
    tech: list[str] = []
    seen = set()

    def add(t: str) -> None:
        if t not in seen:
            seen.add(t)
            tech.append(t)

    for pkg_list in deps.values():
        for pkg in pkg_list:
            low = pkg.lower()
            for kw, label in TECH_KEYWORDS.items():
                if kw in low:
                    add(label)
    # 文件名/目录线索
    all_names = " ".join(
        p.name.lower() for p in repo_dir.rglob("*") if p.is_file()
    )[:5000]
    for kw, label in TECH_KEYWORDS.items():
        if kw in all_names and label not in seen:
            add(label)
    return tech


def _infer_entry_hints(repo_dir: Path, tree: list[str]) -> list[str]:
    hints = []
    # 顶层 + 一层深的常见入口
    candidates = (
        "main.py", "app.py", "manage.py", "cli.py", "run.py", "server.py",
        "index.js", "index.ts", "main.js", "main.ts", "server.js",
        "main.go", "main.rs", "src/main.rs", "lib.rs",
        "src/main.ts", "src/index.tsx", "src/main.java",
    )
    for candidate in candidates:
        if candidate in tree:
            hints.append(candidate)
    # 扫描所有目录下的 cli.py / main.py（如 mfr_recognizer/cli.py）
    for path in tree:
        if path.endswith("/cli.py") or path.endswith("/main.py"):
            if path not in hints:
                hints.append(path)
    return hints[:5]


# 源码 import 语句里的包名 -> 技术栈标签（补充无依赖清单时的识别）
IMPORT_KEYWORDS = {
    "pythonocc": "pythonOCC", "opencascade": "OpenCASCADE", "occ": "OpenCASCADE",
    "numpy": "NumPy", "pandas": "Pandas", "scipy": "SciPy",
    "torch": "PyTorch", "tensorflow": "TensorFlow", "cv2": "OpenCV",
    "opencv": "OpenCV", "sklearn": "scikit-learn", "matplotlib": "Matplotlib",
    "fastapi": "FastAPI", "flask": "Flask", "django": "Django",
    "celery": "Celery", "redis": "Redis", "requests": "Requests",
    "httpx": "httpx", "pydantic": "Pydantic", "sqlalchemy": "SQLAlchemy",
    "pyqt": "PyQt", "pyside": "PySide", "tkinter": "Tkinter",
    "react": "React", "vue": "Vue",
}


def _scan_imports(repo_dir: Path) -> list[str]:
    """扫描 Python/JS 源码的 import 语句，提取提到的包。"""
    found: list[str] = []
    seen = set()
    count = 0
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]
        for f in files:
            ext = Path(f).suffix.lower()
            if ext not in (".py", ".js", ".ts", ".jsx", ".tsx"):
                continue
            try:
                text = (Path(root) / f).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for m in re.finditer(r"^\s*(?:import|from)\s+([A-Za-z0-9_\./]+)", text, re.MULTILINE):
                pkg = m.group(1).split(".")[0].lower()
                if pkg in IMPORT_KEYWORDS and pkg not in seen:
                    seen.add(pkg)
                    found.append(IMPORT_KEYWORDS[pkg])
            count += 1
            if count > 200:
                break
    return found


def _readme_to_html(readme_raw: str) -> str:
    html = md.markdown(
        readme_raw,
        extensions=["extra", "codehilite", "toc", "fenced_code"],
    )
    # 白名单清洗，防 XSS
    allowed_tags = [
        "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
        "ul", "ol", "li", "strong", "em", "b", "i", "code", "pre",
        "blockquote", "a", "img", "table", "thead", "tbody", "tr", "th", "td",
        "span", "div", "sup", "sub",
    ]
    allowed_attrs = {
        "a": ["href", "title"],
        "img": ["src", "alt", "title"],
        "code": ["class"],
        "span": ["class"],
        "div": ["class"],
    }
    return bleach.clean(html, tags=allowed_tags, attributes=allowed_attrs, strip=True)


def analyze(repo_dir: Path) -> dict:
    readme_path = _find_readme(repo_dir)
    readme_raw = ""
    if readme_path:
        readme_raw = readme_path.read_text(encoding="utf-8", errors="ignore")
        if len(readme_raw) > settings.max_readme_chars:
            readme_raw = readme_raw[: settings.max_readme_chars] + "\n\n...(README 已截断)"

    # 项目名/描述：优先用 README 第一个 Markdown 标题（# 开头），
    # 其次第一个非空纯文本行（跳过 HTML 标签行），最后目录名
    readme_title = ""
    readme_first_text = ""
    for line in readme_raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            readme_title = stripped.lstrip("#").strip()
            break
        # 跳过 HTML 标签行（如 <div align="center">）
        if stripped.startswith("<") and ">" in stripped:
            continue
        if not readme_first_text:
            readme_first_text = stripped
    name = readme_title or readme_first_text or repo_dir.name
    description = readme_title or readme_first_text

    languages = _count_languages(repo_dir)
    deps = _parse_deps(repo_dir)
    tree = _build_tree(repo_dir)
    tech_stack = _infer_tech_stack(languages, deps, repo_dir)
    # 补充：从源码 import 提取技术栈（覆盖无依赖清单的项目）
    for t in _scan_imports(repo_dir):
        if t not in tech_stack:
            tech_stack.append(t)
    entry_hints = _infer_entry_hints(repo_dir, tree)
    license_name = _find_license(repo_dir)

    return {
        "name": name,
        "description": description,
        "readme_raw": readme_raw,
        "readme_html": _readme_to_html(readme_raw) if readme_raw else "",
        "languages": languages,
        "tech_stack": tech_stack,
        "dependencies": deps,
        "tree": tree,
        "license": license_name or "未声明",
        "entry_hints": entry_hints,
    }
