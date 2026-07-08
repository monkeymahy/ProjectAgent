# ProjectAgent

技术社区平台：用户提交 Git URL / 压缩包 / 本地路径，后端自动解析项目并用 LLM + Jinja2 模板生成项目展示页，汇总成「最新发布」社区列表。通过 tForum SSO 接入登录，可作为第三方应用嵌入 tForum 站内。

## 架构

```
tForum SSO 登录 → 提交 API → (Celery 队列 / eager 同步) → 解析 → LLM 生成 JSON → Jinja2 渲染 → 静态 HTML
                                                                          ↓
                                                              社区列表页（最新发布）
```

- **解析层** ([app/parsers/analyzer.py](app/parsers/analyzer.py))：clone/解压/复制到本地 → 提取 README、语言、依赖、目录树、技术栈、入口文件、License
- **生成层** ([app/llm/](app/llm/))：LLM 按 JSON schema 生成结构化内容；未配置 key 时用回退生成器
- **渲染层** ([app/llm/renderer.py](app/llm/renderer.py))：LLM 产 JSON + 解析元数据 → Jinja2 模板 → bleach 清洗 → 落盘 HTML
- **沙箱** ([app/sandbox/fetcher.py](app/sandbox/fetcher.py))：大小/深度/超时限制，zip slip 防护
- **SSO** ([app/core/session.py](app/core/session.py))：itsdangerous 签名 cookie；`/sso` 校验 tForum token 后建会话

## 目录结构

```
app/
  api/routes.py        路由：提交、状态、展示页、列表、SSO
  core/config.py       读取 config.yml
  core/session.py      SSO 会话签名/校验
  core/celery_app.py   Celery 应用
  models/models.py     SQLite 表 + 查询
  parsers/analyzer.py  项目解析
  llm/client.py        LLM 调用（OpenAI 兼容）
  llm/renderer.py      Jinja2 渲染 + bleach 清洗
  sandbox/fetcher.py   源码获取（url/zip/local）
  tasks.py             生成任务：parse → generate → render
  templates/           list / progress / project_page / _card
config.example.yml     配置模板（复制为 config.yml 后填写）
requirements.txt       依赖清单
tests/mock_tforum.py   本地测试用的 mock tForum
```

## 配置

复制配置模板并填写：

```bash
cp config.example.yml config.yml
```

关键字段：

| 字段 | 说明 |
|---|---|
| `LLM_API_KEY` | LLM API key；留空则用回退生成器（链路仍可跑通，内容为占位） |
| `LLM_BASE_URL` / `LLM_MODEL` | 任意 OpenAI 兼容接口（ARK / DeepSeek / Moonshot / OpenAI / 本地 vLLM） |
| `EAGER_MODE` | `true`：同步直跑，无需 Redis/Celery（本地测试）；`false`：走 Celery + Redis（生产） |
| `TFORUM_BASE_URL` | tForum 后端地址，用于服务端调 `verifyToken` |
| `SSO_COOKIE_SECRET` | 签会话 cookie 的密钥，**生产必须改成随机长串** |
| `SSO_SESSION_TTL` | 会话有效期（秒），默认 7 天 |
| `PROJECTAGENT_PUBLIC_URL` | 本应用对外地址，用于在 tForum 配置外部栏目 URL |

> `config.yml` 已在 `.gitignore` 中，含真实密钥不会被提交。

## 安装

依赖用 conda 管理，环境名 `projshow`，Python 3.11：

```bash
conda create -n projshow python=3.11 -y
conda run -n projshow pip install -r requirements.txt
```

## 运行

### 本地测试（eager 模式，无需 Redis）

确认 `config.yml` 里 `EAGER_MODE: true`，然后：

```bash
# Windows（用环境 python 直起，避免 conda run 的中文编码问题）
F:/miniforge/envs/projshow/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765

# Linux / macOS
conda run -n projshow uvicorn app.main:app --host 0.0.0.0 --port 8765
```

启动后会自动建库（`storage/projshow.db`）和目录。

### 生产模式（Celery + Redis）

`config.yml` 设 `EAGER_MODE: false`，先起 Redis，再起 API 和 worker：

```bash
redis-server &
conda run -n projshow uvicorn app.main:app --host 0.0.0.0 --port 8765
conda run -n projshow celery -A app.core.celery_app worker -l info
```

## SSO：接入 tForum

接入流程参考 `第三方应用集成方案.md`：

1. **tForum 侧**：管理后台配置一个外部栏目，URL 填
   ```
   {PROJECTAGENT_PUBLIC_URL}/sso?token={token}
   ```
   用户在 tForum 站内点击该栏目时，tForum 前端把 `{token}` 替换为当前用户 token，`window.open(finalUrl, '_blank')` 打开本应用。

2. **本应用侧**：`GET /sso?token=xxx` 收到 token 后，服务端调
   `GET {TFORUM_BASE_URL}/api/v1/user/verifyToken?token=xxx` 校验：
   - `code == 0`：用返回的用户信息 `upsert_user`，签发 `pa_session` cookie，303 跳转 `/`
   - `code != 0` / 无 token / 连接失败：返回登录失败提示页

3. **登录态**：后续请求由 `_current_user` 依赖从 cookie 解出用户。提交项目接口（`/projects/url|local|upload`）要求登录，否则 401；项目自动归属当前用户。

### 本地测试 SSO（无真实 tForum 时）

仓库自带 mock tForum（[tests/mock_tforum.py](tests/mock_tforum.py)），对 `verifyToken` 返回假用户 `10086 测试用户`，`token=bad` 返回失败。另起一个终端：

```bash
F:/miniforge/envs/projshow/python.exe tests/mock_tforum.py   # 监听 :8081
```

`config.yml` 保持 `TFORUM_BASE_URL: "http://localhost:8081"`，然后浏览器访问：

```
http://localhost:8765/sso?token=goodtok    # 模拟从 tForum 跳转过来
```

成功后会被跳回首页并已登录，topbar 显示用户名，发布 Modal 显示「发布身份」。

## API

| 方法 | 路径 | 登录 | 说明 |
|---|---|---|---|
| GET | `/` | – | 社区列表页（最新发布） |
| GET | `/projects` | – | 项目列表 JSON（分页 + 语言/标签筛选） |
| POST | `/projects/url` | ✅ | body `{"url":"https://gitee.com/x/y.git"}` |
| POST | `/projects/local` | ✅ | form: `path=D:/...` |
| POST | `/projects/upload` | ✅ | multipart: zip 文件 |
| GET | `/projects/{id}/status` | – | 查状态 |
| GET | `/projects/{id}/status/stream` | – | SSE 状态推送 |
| GET | `/projects/{id}/progress` | – | 生成进度页 |
| GET | `/projects/{id}/page` | – | 查看生成的展示页 HTML |
| GET | `/sso?token=xxx` | – | tForum SSO 入口，校验后建会话 |
| GET | `/me` | – | 探测当前登录用户 |
| POST | `/logout` | – | 清除会话 |
| GET | `/health` | – | 健康检查 |

## 部署清单（生产）

- [ ] `config.yml`：`EAGER_MODE: false`，`REDIS_URL` 指向真实 Redis
- [ ] `LLM_API_KEY` 填真实 key
- [ ] `TFORUM_BASE_URL` 改成真实 tForum 地址
- [ ] `SSO_COOKIE_SECRET` 换成随机长串（如 `python -c "import secrets;print(secrets.token_urlsafe(48))"`）
- [ ] `PROJECTAGENT_PUBLIC_URL` 改成对外 https 地址
- [ ] 反向代理走 **HTTPS**，并把 [app/api/routes.py](app/api/routes.py) 中 `/sso` 的 `set_cookie(secure=False)` 改成 `secure=True`
- [ ] tForum 管理后台配好外部栏目 URL
- [ ] 用 systemd / supervisor 守护 uvicorn 与 celery worker
- [ ] `storage/` 目录做好备份（SQLite + 生成页）；如需扩容可换 MySQL/PG（改 `DATABASE_URL` 并适配 [app/models/models.py](app/models/models.py)）

## 安全要点

- clone/解压在受限目录，有大小/超时上限；zip slip 路径校验
- README 渲染走 markdown + bleach 白名单
- LLM 只产 JSON 不产 HTML；模板渲染 + bleach 清洗文本字段，杜绝 XSS
- 会话 cookie 为 `httponly` + 签名（itsdangerous），防篡改
- 提交接口强制登录，项目归属可追溯（`owner_id`）
