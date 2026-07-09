# ProjectAgent 流水线文档

从读取项目源码到输出展示页的完整流程、各环节的数据结构与算法，以及同步更新时的分级路由（L0/L1/L2/全量）。

---

## 1. 总览

### 1.1 架构

```
tForum SSO ──> FastAPI 路由 ──> (eager 线程 / Celery) ──> _run_pipeline
                                                            │
                 ┌──────────────────────────────────────────┘
                 ▼
  fetch源码 ──> analyze解析 ──> [分级路由] ──> render渲染 ──> 原子落盘 HTML
                   │              │                │
                   │              │                └─> upsert_card（社区卡片）
                   │              └─> L0跳过 / L1复用 / L2增量 / 全量
                   └─> source_hash + file_hashes + code_structure
                                                            │
                                                            ▼
                                                   SQLite（4 张表）
```

### 1.2 两种触发路径

| 触发 | 入口 API | 任务 | 说明 |
|---|---|---|---|
| 首次提交 | `POST /projects/{url,local,upload}` | `process_project` | 无 `source_hash`，走全量 |
| 同步更新 | `POST /projects/{id}/sync` | `sync_project` | 有 `source_hash`，走分级路由 |
| 在线编辑 | `PUT /projects/{id}` | （同步，无任务） | 改单个字段，重渲染 |

两个 Celery 任务都调同一个 [`_run_pipeline`](../app/tasks.py)，区别仅在于 DB 里是否已有 `source_hash`——分级路由据此判断首次还是更新。

### 1.3 核心数据流

```
源码目录
  │  fetcher 获取
  ▼
repo_dir (storage/repos/{id}/)
  │  analyzer.analyze
  ▼
ParsedProject (dict) ──────────> source_hash / file_hashes
  │  build_prompt                │
  ▼                              │
LLM prompt (str)                 │
  │  _http_chat                  │
  ▼                              │
generated JSON (8 字段) <─ L2增量时合并旧 generated
  │  render_page
  ▼
HTML (str) ──原子写──> storage/pages/{id}.html
  │
  └─> upsert_card ──> project_cards 表
```

---

## 2. 系统组件与存储

### 2.1 目录结构

```
ProjectAgent/
├── app/
│   ├── main.py              FastAPI app + 启动（日志/建表/建目录）
│   ├── api/routes.py        全部路由（提交/同步/编辑/删除/SSO/列表）
│   ├── core/
│   │   ├── config.py        Settings（读 config.yml）
│   │   ├── celery_app.py    Celery 实例（eager 模式下不用）
│   │   └── session.py       SSO cookie 签名/校验
│   ├── models/models.py     SQLite 表 + 全部 DB 操作
│   ├── sandbox/fetcher.py   源码获取（url/zip/local）
│   ├── parsers/analyzer.py  解析 + hash + tree-sitter + 分类
│   ├── llm/
│   │   ├── schema.py        prompt 模板 + OUTPUT_SCHEMA
│   │   ├── client.py        LLM 调用 + JSON 解析 + 回退
│   │   └── renderer.py      Jinja2 渲染 + bleach 清洗
│   └── templates/           Jinja2 模板（project_page.html 等）
├── storage/                 运行时数据（gitignore）
│   ├── projshow.db          SQLite
│   ├── repos/{id}/          解压后的源码
│   ├── uploads/{id}.zip     用户上传的 zip
│   ├── pages/{id}.html      生成的展示页
│   └── logs/app.log         按天滚动日志
└── config.yml               配置（含密钥，gitignore）
```

### 2.2 配置 [`config.py`](../app/core/config.py)

`Settings` 从 `config.yml` 读，关键项：

| 配置 | 默认 | 作用 |
|---|---|---|
| `EAGER_MODE` | true | true=后台线程跑任务，false=丢 Celery |
| `LLM_MAX_TOKENS` | 8000 | LLM **输出**上限（非上下文窗口；config.yml 可覆盖，128k 上下文下不宜设过大以免挤占输入） |
| `LLM_TEMPERATURE` | 0.4 | |
| `MAX_REPO_SIZE_MB` | 200 | 源码大小上限 |
| `MAX_TREE_ENTRIES` | 100000 | 目录树最多条目（默认相当于不限） |
| `MAX_README_CHARS` | 1000000 | README 截断长度（默认相当于不限） |
| `CLONE_DEPTH` | 1 | git clone 深度 |

### 2.3 数据库表 [`models.py`](../app/models/models.py)

SQLite，4 张表：

**`projects`**——项目主表，一行一个项目：

| 列 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | 12 位 uuid hex |
| source_type | TEXT | `url` / `zip` / `local` |
| source | TEXT | URL / zip 路径 / 本地路径 |
| owner_name / owner_id | | 提交者（tForum 用户） |
| status | TEXT | `pending`/`cloning`/`parsing`/`generating`/`done`/`failed` |
| progress | INT | 0-100，失败=-1 |
| message | TEXT | 进度提示文案 |
| parsed_json | TEXT | `ParsedProject` 序列化 |
| generated_json | TEXT | LLM 输出的 8 字段 JSON |
| html_path | TEXT | `storage/pages/{id}.html` |
| source_hash | TEXT | 整体源码指纹（L0 依据） |
| template_version | INT | 模板版本（老页升级用） |
| error | TEXT | 失败原因 |
| created_at / updated_at | TEXT | UTC iso |

**`project_cards`**——社区列表页卡片摘要，`upsert_card` 平铺写入（便于分页查询不用反序列化每个项目的 JSON）：

| 列 | 说明 |
|---|---|
| project_id PK | 外键 projects.id |
| title / summary | 从 generated 取并 `_clean_text` 剥标签 |
| primary_lang | languages 第一个 |
| lang_json / tech_stack_json / tags_json | JSON 字符串 |
| card_color | 按 primary_lang 查 `LANG_COLORS` |
| owner_name / owner_id / created_at | |

**`file_hashes`**——文件级指纹（L1/L2 依据）：

```
(project_id, rel_path) PK, hash
```

**`users`**——tForum SSO 用户缓存。

### 2.4 日志 [`main.py`](../app/main.py)

`_setup_logging` 在 startup 最先调用：`app` 命名空间 logger，INFO 级，控制台 + `storage/logs/app.log`（按天滚动，保留 7 天），`propagate=False` 不冒泡到 uvicorn。所有模块 `logging.getLogger(__name__)` 拿到的都是 `app.xxx` 子 logger。

---

## 3. 首次生成流程（逐步）

以 `POST /projects/upload`（zip 上传）为例，见 [`routes.py`](../app/api/routes.py)。

### 步骤 0：鉴权 + 建项目记录

- `_require_user` 从 cookie 解出 tForum 用户，未登录 401。
- 生成 `project_id = uuid4().hex[:12]`。
- zip 流式写到 `storage/uploads/{id}.zip`（每块 1MB）。
- `create_project` 写一行 `projects`，status=`pending`。
- `_dispatch`：eager 模式开 daemon 线程跑 `process_project.run(id)`，否则 `task.delay(id)`。
- 接口立即返回 `{project_id, status:"pending"}`，前端跳进度页。

### 步骤 1：`_run_pipeline` 启动 [`tasks.py:77`](../app/tasks.py)

```python
update_status(project_id, CLONING, 10, "正在获取项目源码...")
if repo_dir.exists(): shutil.rmtree(repo_dir)   # 覆盖旧目录
_fetch_source(source_type, source, repo_dir)
```

### 步骤 2：源码获取 [`fetcher.py`](../app/sandbox/fetcher.py)

| source_type | 函数 | 算法 |
|---|---|---|
| url | `fetch_url` | `git clone --depth=N --single-branch`，超时/失败转 `FetchError` |
| zip | `fetch_zip` | `zipfile` 解压，**zip slip 防护**（每个 name 校验 `resolve()` 不逃出 dest），单顶层目录下钻一层 |
| local | `fetch_local` | `shutil.copytree` + `ignore_patterns` |

三者最后都过：
- `_remove_git_and_ignored`：删 `.git`/`node_modules`/`__pycache__`/`dist` 等（减体积）。
- `_check_size`：遍历累加文件大小，超 `MAX_REPO_SIZE_MB` 抛 `FetchError`。

### 步骤 3：解析 [`analyzer.py:320`](../app/parsers/analyzer.py) `analyze(repo_dir) -> ParsedProject`

见 [§4.2](#42-解析-analyzer)。产出 `ParsedProject` dict。

### 步骤 4：LLM 生成 [`client.py`](../app/llm/client.py) `generate(parsed) -> generated`

见 [§4.3](#43-llm-生成-clientschema)。产出 8 字段 JSON。

### 步骤 5：渲染 [`renderer.py`](../app/llm/renderer.py) `render_page(parsed, generated, ...) -> html`

见 [§4.4](#44-渲染-renderer)。Jinja2 套模板 + bleach 清洗。

### 步骤 6：原子落盘 [`tasks.py:58`](../app/tasks.py)

```python
page_path = settings.pages_dir / f"{project_id}.html"
_atomic_write_html(page_path, html)   # 先写 .tmp 再 os.replace
```

### 步骤 7：写卡片 + 文件指纹

```python
upsert_card(project_id, parsed, generated, owner_name, owner_id)
save_file_hashes(project_id, new_file_hashes)
update_status(project_id, DONE, 100, "完成",
              parsed_json=parsed, generated_json=generated,
              html_path=str(page_path), source_hash=new_hash)
```

`update_status` 用可变 `**fields`：dict/list 值自动 `json.dumps`，标量原样写。

---

## 4. 各环节算法与数据结构

### 4.1 源码获取 fetcher（见 §3 步骤 2）

**zip slip 防护算法**（`fetch_zip`）：
```
for name in zf.namelist():
    target = (dest / name).resolve()
    if not str(target).startswith(str(dest.resolve())):
        raise FetchError("zip slip")
```
保证解压路径不逃出 dest。

**单顶层目录下钻**：解压后若只有一个非隐藏子目录（常见于 `proj-main/` 包裹），把内容上移一层再删空目录，保证 `repo_dir` 直接就是项目根。

### 4.2 解析 analyzer

`analyze(repo_dir)` 返回 **`ParsedProject`**：

```python
{
  "name": str,              # README 一级标题 / 首行文本 / 目录名
  "description": str,        # README 标题或首行
  "readme_raw": str,         # README 原文（截断到 MAX_README_CHARS）
  "readme_html": str,        # markdown→HTML，bleach 白名单清洗
  "languages": {lang: count},# 按文件数倒序
  "tech_stack": [str],       # 依赖关键词 + import 关键词推断
  "dependencies": {eco: [pkg]},  # ecosystem: node/python/go/rust/maven/...
  "tree": [path, ...],       # 扁平 posix 相对路径，≤ MAX_TREE_ENTRIES
  "license": str,            # MIT/Apache-2.0/BSD/GPL/文件名/"未声明"
  "entry_hints": [str],      # 入口文件候选，≤5
  "code_structure": {rel: [symbol]},  # tree-sitter 全量提取（不限文件数）
}
```

各子算法：

| 函数 | 算法 |
|---|---|
| `_find_readme` | 按优先序找 README.md/.rst/.txt/README/readme.md，再扫首字母 readme 的文件 |
| `_find_license` | 找 LICENSE 等文件，读前 200-400 字符关键词匹配 MIT/Apache/BSD/GPL |
| `_build_tree` | `os.walk`，`IGNORE_DIRS` 过滤，隐藏文件过滤（保留 .gitignore/.env.example），`max_tree_entries` 截断 |
| `_count_languages` | 按扩展名查 `EXT_LANG` 表，统计计数倒序 |
| `_parse_deps` | 遍历 `DEP_FILES` 表，按格式调 `_extract_deps` |
| `_extract_deps` | 各格式解析器：package.json 用 `json.loads` 取 dependencies/devDependencies；requirements.txt 正则；go.mod 取 require 行；Cargo.toml 取 `[dependencies]` 段；pom.xml/gradle 取 artifactId/implementation；Gemfile 取 gem；Pipfile/pyproject 正则 |
| `_infer_tech_stack` | 依赖包名匹配 `TECH_KEYWORDS` + 文件名 rglob 关键词 |
| `_scan_imports` | 扫 .py/.js/.ts 源码 `import/from` 语句，匹配 `IMPORT_KEYWORDS` 补全技术栈（覆盖无依赖清单项目） |
| `_infer_entry_hints` | 候选入口名表（main.py/app.py/index.ts...）+ 目录下 cli.py/main.py，≤5 |
| `_readme_to_html` | `markdown` 库渲染 + `bleach.clean` 白名单标签/属性，防 XSS |

**hash 算法**（核心，分级路由依据）：

`compute_source_hash(repo_dir)`——整体指纹：
```
h = sha256()
for rel in sorted(walk_repo_files(repo)):   # 路径排序保证稳定
    h.update(rel.encode()); h.update(b"\0")
    h.update(file_content_bytes); h.update(b"\0")
return h.hexdigest()
```
纳入**路径 + 内容**，能反映重命名与内容修改。

`compute_file_hashes(repo_dir)`——文件级指纹：`{rel: sha256(content)}`，只看内容不看路径。L1/L2 用。

`_walk_repo_files` 是两者共享的遍历器，过滤规则与 `_build_tree` 一致（保证 hash 范围与展示的 tree 范围对齐）。

**`is_non_critical(rel_path)`**——文件关键性分类器（L1 依据）：

```
依赖清单(DEP_FILES)         -> 关键（影响 tech_stack/dependencies）
README*                     -> 关键（展示内容源）
非关键目录(tests/docs/.github/examples/...) 下 -> 非关键
LICENSE/CHANGELOG/.gitignore 等 -> 非关键
.md/.rst/.txt/.lock/.log    -> 非关键
其余（源码/配置）            -> 关键
```

**`extract_code_structure(repo_dir)`**——tree-sitter 符号提取（阶段3）：

```
for rel in walk_repo_files(repo):
    ext = 扩展名
    pq = _ts_parser(ext)        # lru_cache 缓存 (Parser, Query)
    if pq is None or is_non_critical(rel): continue
    tree = parser.parse(content_bytes)
    caps = QueryCursor(query).captures(tree.root_node)  # {tag: [Node]}
    items = [(start_byte, label, name) for tag,nodes in caps ...]
    items.sort()                          # 按源码出现顺序
    result[rel] = [f"{label} {name}" for ... in items]  # 全量符号，不截断
```

`_ts_parser(ext)` 按扩展名返回 `(Parser, Query)`，lru_cache：
- `.py`：tree-sitter-python，捕 `function_definition`/`class_definition`
- `.js`/`.jsx`：tree-sitter-javascript，捕 `function_declaration`/`class_declaration`/`method_definition`
- `.ts`：tree-sitter-typescript(`language_typescript()`)
- `.tsx`：tree-sitter-typescript(`language_tsx()`)

> tree-sitter 0.26 原生 API：`Parser(Language(pack.language()))` + `Query(lang, source)` + `QueryCursor(query).captures(node)` 返回 `{tag: [Node]}` dict（不是旧版的 tuple 迭代）。

**`affected_fields(changed_files)`**——文件→字段映射（L2 依据）：

```
README*    -> [title, one_line_summary, getting_started, highlights]
依赖清单   -> [tech_stack]
其余源码   -> [architecture_overview, highlights, use_cases]
```
取并集后排序返回。

### 4.3 LLM 生成 client + schema

**OUTPUT_SCHEMA**（[`schema.py`](../app/llm/schema.py)，LLM 必须输出的 8 字段）：

```python
{
  "title": str,
  "one_line_summary": str,
  "highlights": [str],          # 3-5 条
  "tech_stack": [str],
  "architecture_overview": str, # 2-4 段
  "use_cases": [str],
  "getting_started": str,
  "tags": [str],                # 3-6 个
}
```

**`build_prompt(parsed)`**——拼全量 prompt：
- `_build_meta`：精简 metadata。`limits=None` 时全量；压缩重试时按 `COMPACT_LIMITS` 截断。
- `_build_code_structure_text`：代码结构格式化文本。`limits=None` 时全量；压缩重试时按 `COMPACT_LIMITS`（100 文件/40 符号/40000 字符）截断。
- `_build_context_hints`：按元数据特征动态生成提示（无 README/无依赖清单/无入口/无 license 时引导 LLM 留空而非瞎编）。
- 套 `PROMPT_TEMPLATE`：硬性要求（不编造/信息不足留空/纯 JSON）+ schema + metadata + 代码结构 + 上下文提示。

**`_http_chat(prompt)`**——发 OpenAI 兼容请求：
```
POST {base_url}/chat/completions
headers: Authorization: Bearer {api_key}
body: {model, messages:[system,user], temperature, max_tokens}
```
日志：请求（model/prompt_len）、响应（耗时/content_len/prompt·completion·total token）、HTTP 错误、调用失败。

**`_extract_json(text)`**——JSON 容错解析，三级降级：
1. 去掉 ` ``` ` 围栏后 `json.loads`。
2. 去掉对象/数组尾逗号（`,}`→`}`）再 parse。
3. 正则抠第一个 `{...}` 块再试。

**`call_llm(prompt)`**：调 `_http_chat` + `_extract_json`；首次解析失败则改写 retry_prompt（提醒严格 JSON）重试一次。

**`generate(parsed)`**：`call_llm(build_prompt(parsed))` 全量调用；若报上下文超限（`_is_context_too_large` 关键词匹配 "context/length/too long/maximum/token limit/too many"），改用 `build_prompt(parsed, COMPACT_LIMITS)` 压缩重试一次；仍失败或未配 key 则 `fallback_generate`（纯元数据拼占位内容，保证链路可跑）。

**`generate_incremental(parsed, old_generated, changed_files, affected_fields)`**（L2）：
- `build_incremental_prompt`：喂旧 generated + 变化文件 + 受影响字段 + 最新 metadata + 最新 code_structure，要求**只输出受影响字段**。
- 调 `call_llm`，失败返回 `{}`（调用方保持旧值，不降级全量）。

### 4.4 渲染 renderer

**`render_page(parsed, generated, project_id, source, source_type)`**：
- `_sanitize_text_fields(generated)`：对纯文本字段先 `re.sub(r"<[^>]+>","")` 剥标签，再 `bleach.clean(tags=[], strip=True)` 彻底去 HTML；列表字段逐元素清洗。防 LLM 注入标签/脚本。
- `_build_tree_text(tree)`：扁平路径列表 → 缩进树形文本（`├──`/`└──`/`│`），用 dict 构建嵌套结构，按“目录优先排序”渲染。
- 套 `project_page.html` 模板，传入 generated 8 字段 + parsed 元数据 + 源码链接 + `template_version`。
- `autoescape` 开启，Jinja2 自动转义。

**`_atomic_write_html`**（[`tasks.py:58`](../app/tasks.py)）：先写 `page.tmp` 再 `os.replace` 原子替换，避免生成中途崩溃留下半截 HTML。

**`upsert_card`**（[`models.py:227`](../app/models/models.py)）：
- `primary_lang` = languages 第一个，`card_color` 查 `LANG_COLORS`。
- title/summary 经 `_clean_text`（剥标签 + 压缩空白）并截断（title≤120，summary≤300）。
- `INSERT ... ON CONFLICT(project_id) DO UPDATE`，复用 `projects.created_at` 保持列表顺序。

---

## 5. 同步更新流程（分级路由 L0/L1/L2/全量）

入口 `POST /projects/{id}/sync`（[`routes.py:419`](../app/api/routes.py)）：

1. 鉴权（作者或管理员）。
2. 状态校验：正在 `cloning/parsing/generating` 时返回 409 拒绝重复触发。
3. **zip 项目**必须上传新 zip，流式覆盖 `storage/uploads/{id}.zip`。
4. `update_status(PENDING, 0, "准备同步更新...")`，`_dispatch(sync_project)`。

任务进 `_run_pipeline`，fetch 完源码后进入分级决策（[`tasks.py:94-182`](../app/tasks.py)）：

### 5.1 决策树

```
new_hash = compute_source_hash(repo)
old_hash = project.source_hash

┌─ L0：源码无变化 ─────────────────────────────────────────┐
│ if old_hash and new_hash == old_hash:                    │
│     DONE, "源码无变化，已是最新"   # 不调 LLM，不解析     │
│     return                                               │
└──────────────────────────────────────────────────────────┘

new_file_hashes = compute_file_hashes(repo)
old_file_hashes = get_file_hashes(project)      # DB 读
changed = _diff_file_hashes(old, new)          # 新增/修改/删除并集

┌─ L1：仅非关键文件变化 ───────────────────────────────────┐
│ if old_hash and generated_json and changed                │
│    and all(is_non_critical(rel) for rel in changed):      │
│     parsed = analyze(repo)            # 重新解析（tree等变了）│
│     generated = old generated_json    # 复用，不调 LLM      │
│     render + 原子落盘 + upsert_card                       │
│     save_file_hashes(new)                                 │
│     DONE, "非关键文件变化，已跳过 LLM 更新"               │
│     return                                               │
└──────────────────────────────────────────────────────────┘

critical_changed = [rel for rel in changed if not is_non_critical(rel)]

┌─ L2：关键文件少量变化（≤5） ─────────────────────────────┐
│ if old_hash and generated_json and critical_changed       │
│    and len(critical_changed) <= 5:                        │
│     parsed = analyze(repo)                                │
│     affected = affected_fields(critical_changed)          │
│     incremental = generate_incremental(                   │
│         parsed, old_generated, critical_changed, affected)│
│     filtered = {k:v for k,v in incremental if k in affected}│
│     new_generated = {**old_generated, **filtered}  # 合并  │
│     render + 落盘 + upsert_card + save_file_hashes        │
│     DONE, "增量更新完成"（或"增量 LLM 失败，保持旧内容"） │
│     return                                               │
└──────────────────────────────────────────────────────────┘

┌─ 全量重生 ───────────────────────────────────────────────┐
│ 关键文件 >5 个，或首次生成（old_hash=None）               │
│ parsed = analyze(repo)                                    │
│ generated = generate(parsed)       # 调全量 LLM           │
│ render + 落盘 + upsert_card + save_file_hashes            │
│ DONE, "完成"                                              │
└──────────────────────────────────────────────────────────┘
```

### 5.2 分级设计意图

| 级别 | 触发条件 | 是否调 LLM | 产物复用 | 省 token |
|---|---|---|---|---|
| L0 | source_hash 完全相等 | 否 | 全复用 | ★★★ |
| L1 | 仅 tests/docs/CI/许可证/纯文档变化 | 否 | 复用 generated_json | ★★ |
| L2 | 关键文件变化 ≤5 | 是（只更受影响字段） | 旧 generated + 增量合并 | ★ |
| 全量 | 关键文件 >5，或首次 | 是（全量） | 不复用 | 0 |

设计目标是**成本敏感**：绝大多数日常提交（改测试、改文档、小修源码）都不走全量 LLM。

### 5.3 `_diff_file_hashes(old, new)` 算法

```
changed = set()
for rel, h in new.items():
    if old.get(rel) != h: changed.add(rel)   # 新增或内容变
for rel in old:
    if rel not in new: changed.add(rel)      # 删除
return sorted(changed)
```
按 rel 比对，所以**文件重命名**会被识别为“旧路径删除 + 新路径新增”（即使内容不变），计入 changed。

### 5.4 L2 的字段过滤与合并

```python
incremental = generate_incremental(...)   # LLM 可能返回多余字段
filtered = {k: v for k, v in incremental.items() if k in affected}
new_generated = {**old_generated, **filtered} if filtered else old_generated
```
- **过滤**：只接受 `affected_fields` 列出的字段，丢弃 LLM 多输出的，防止越权改其他字段。
- **合并**：`{**old, **filtered}` 用增量覆盖旧值；`filtered` 为空（LLM 失败）时保持旧 generated 不降级。
- 状态文案区分：`filtered` 非空→“增量更新完成”；空→“增量 LLM 失败，保持旧内容”。

### 5.5 首次生成的边界

`old_hash=None` 时，L0/L1/L2 三个分支的条件都含 `if old_hash`，全部跳过，直接全量。这保证**首次必须有完整 LLM 生成**，后续才有增量基线。

---

## 6. 在线编辑流程 [`routes.py:469`](../app/api/routes.py)

`PUT /projects/{id}`，body `{field, value}`：

1. 鉴权（作者/管理员）。
2. `field` 必须在 `EDITABLE_FIELDS`（8 字段全集）；列表字段校验 value 是 list，文本字段校验是 str。
3. 读 `parsed_json` + `generated_json`，改 `gen[field] = value`。
4. `update_generated` 写回 `generated_json`。
5. `upsert_card` 刷新卡片摘要（title/summary/tags 等）。
6. `render_page` 重渲染，直接 `Path(html_path).write_text`（非原子，编辑是低频操作）。

不经过任务队列、不调 LLM、不动 source_hash/file_hashes。展示页前端用 `_AUTH_SNIPPET` 注入的“✎ 编辑”按钮触发。

---

## 7. 删除流程 [`routes.py:389`](../app/api/routes.py)

`DELETE /projects/{id}`：
1. 鉴权（作者/管理员）。
2. `delete_project`（DB）：清 `project_cards` + `file_hashes` + `projects` 三表行。
3. 删 `storage/pages/{id}.html`。
4. 删 `storage/repos/{id}/` 目录。
5. 删 `storage/uploads/{id}.zip`。

---

## 8. 展示页注入与查看 [`routes.py:305`](../app/api/routes.py)

`GET /projects/{id}/page`：
- status≠done 返回 409。
- 读 HTML 文件；若缺 `data-field`/`pa-src-links` 标记（老页面），用存储的 parsed_json + generated_json **重新渲染升级**并落盘。
- 若当前用户可改（作者/管理员），`_inject_auth_tools` 在 `</body>` 前注入：
  - “同步更新”按钮：zip 项目弹文件选择器上传新 zip；url/local 直接 POST。
  - “删除项目”按钮。
  - 各字段的“✎ 编辑”按钮（按 `data-field` 自动挂载）。
- 未登录或非作者只看纯展示页，无操作按钮。

---

## 9. 数据结构速查表

### ParsedProject（`analyze` 返回 / `parsed_json` 存储）
见 [§4.2](#42-解析-analyzer)。11 个字段。

### generated JSON（`generated_json` 存储 / OUTPUT_SCHEMA）
见 [§4.3](#43-llm-生成-clientschema)。8 个字段。

### projects 行关键字段
`id, source_type, source, owner_{name,id}, status, progress, message, parsed_json, generated_json, html_path, source_hash, template_version, error, created_at, updated_at`

### project_cards 行
`project_id, title, summary, primary_lang, lang_json, tech_stack_json, tags_json, card_color, owner_{name,id}, created_at`

### file_hashes 行
`project_id, rel_path, hash`（PK = project_id + rel_path）

### TaskStatus 枚举
`PENDING → CLONING → PARSING → GENERATING → DONE`（失败 `FAILED`，progress=-1）

---

## 10. 关键边界与已知风险

| 点 | 说明 |
|---|---|
| **max_tokens 上限** | 默认 8000（config.yml 可覆盖）。此为**输出**上限，非上下文窗口；128k 上下文下不宜设过大以免挤占输入。 |
| **全量提取 prompt 体积** | 不再截断文件/符号数量，大仓库 prompt 可能超上下文；由 `generate` 的压缩重试（`COMPACT_LIMITS`）兜底，仍失败则 `fallback_generate`。 |
| **tree-sitter 语言覆盖** | 支持 14 语言 19 扩展（.py/.js/.jsx/.ts/.tsx/.go/.rs/.java/.kt/.c/.h/.cpp/.cc/.hpp/.cs/.rb/.php/.sh/.bash），其他语言 `code_structure` 为空，LLM 退回靠 README+tree。 |
| **符号标签语言感知** | `_TS_LANGUAGES` 表为每种语言配独立 labels（Python `def`、Rust `fn`、Go `func`、Kotlin `fun`、Java `method` 等），不再是 Python 中心。 |
| **重命名计入变化** | `_diff_file_hashes` 按 rel 比对，文件重命名算删+增，可能把 L1 推成 L2（若新路径是关键文件）。 |
| **L2 上限 5 个关键文件** | 超过则全量，避免增量 prompt 过大或字段漂移。 |
| **eager 模式线程** | 默认 `EAGER_MODE=true`，任务在 daemon 线程跑，进程退出则任务丢失；生产应切 Celery + worker。 |
| **SSO cookie secure=False** | 本地 http 调试，生产 https 必须改 `secure=True`。 |
| **zip slip / 大小限制** | `fetch_zip` 强制路径校验，`_check_size` 限 `MAX_REPO_SIZE_MB`，防恶意包。 |
