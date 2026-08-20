# GitLab Webhook 配置指南

本站通过 GitLab 的 **Push Webhook** 实时感知工具库 / Skill 库仓库的更新，收到推送事件后自动 `git pull` 并刷新 `/library` 页面数据，无需轮询。

需要**仓库管理员（Maintainer 及以上权限）**在两个源仓库上各配置一个 Webhook。Developer 权限无法看到 Webhook 设置页，请转发本文档给管理员操作。

---

## 一、准备工作：本站侧配置

先在本站 `config.yml` 中确认以下配置（`PROJECTAGENT_PUBLIC_URL` 必须是 GitLab 服务器能访问到的地址）：

```yaml
PROJECTAGENT_PUBLIC_URL: "http://your-server:8765"   # GitLab 必须可达
LIBRARY_WEBHOOK_SECRET: "一串随机字符串"              # 与 GitLab 里的 Secret token 保持一致
```

生成随机密钥示例：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 二、在 GitLab 仓库上添加 Webhook

对**工具库仓库**和 **Skill 库仓库**各执行一次（步骤完全相同）：

1. 打开仓库页面，进入 **Settings → Webhooks**
   （左侧菜单：Settings → Webhooks；旧版为 Settings → Integrations）
2. 填写表单：

   | 字段 | 填写内容 |
   |---|---|
   | **URL** | `http://your-server:8765/integrations/gitlab-webhook` |
   | **Secret token** | 与 `LIBRARY_WEBHOOK_SECRET` 完全一致的字符串 |
   | **Trigger** | 只勾选 **Push events** |
   | **SSL verification** | 本站无 HTTPS 时选 **Disable**；有 HTTPS 且证书有效可选 **Enable** |

3. 点击 **Add webhook** 保存。

## 三、验证 Webhook 是否生效

1. 保存后页面下方会出现刚添加的 Webhook，点击 **Test → Push events**。
2. GitLab 会发送一个模拟推送事件，查看返回结果：
   - **HTTP 200**，响应体形如 `{"ok": true, "matched": "tool", "branch": "master"}` —— 配置成功。
     `matched` 为 `"tool"` 或 `"skill"` 表示本站正确识别到了对应仓库。
   - `{"ok": true, "matched": null}` —— Webhook 通了，但事件里的仓库地址与 `config.yml` 中配置的 `LIBRARY_TOOL_REPO_URL` / `LIBRARY_SKILL_REPO_URL` 不匹配（Test 事件用的是真实仓库地址，若返回 null 请检查这几个 URL 是否指向同一仓库）。
   - **HTTP 403** `无效的 webhook token` —— Secret token 与 `LIBRARY_WEBHOOK_SECRET` 不一致。
   - **HTTP 400** `Webhook 未配置` —— 本站 `LIBRARY_WEBHOOK_SECRET` 为空。
   - **连接失败 / 超时** —— GitLab 服务器无法访问 `PROJECTAGENT_PUBLIC_URL`，检查网络、防火墙和端口。
3. 也可以查看 Webhook 列表中的 **Recent events**，点开可看到每次推送的请求详情和本站返回。

## 四、工作原理

- 只要有分支收到 push，GitLab 就会 POST 事件到 `/integrations/gitlab-webhook`。
- 本站校验 `X-Gitlab-Token` 密钥 → 匹配事件中的仓库地址 → 后台执行一次 `git pull` + 解析 + 缓存刷新。
- 刷新在后台线程进行，**接口立即返回 200**，不会让 GitLab 超时重试。
- 若推送时恰有刷新正在进行，本站会等它结束后再刷新一次，事件不会丢失。
- 非本站关注的仓库、非 push 事件（如 tag、issue）会被直接忽略（返回 200，避免 GitLab 报错重试）。

## 五、常见问题

| 现象 | 原因 / 处理 |
|---|---|
| **"Url is blocked: Requests to the local network are not allowed"** | GitLab 默认禁止 webhook 请求内网地址。自建 GitLab 让管理员在 **Admin Area -> Settings -> Network -> Outbound requests** 勾选允许(或将本站地址加入 allowlist);gitlab.com 无法放行,需本站有公网地址 |
| GitLab 显示 403 | Secret token 不一致，逐字符核对两边配置 |
| 显示 200 但 `/library` 没更新 | 看 `matched` 是否为 null（仓库 URL 不匹配）；或看本站日志中是否有 `GitLab webhook 触发库刷新` 与 git pull 报错 |
| 本站重启后第一次没数据 | 正常，首次访问 `/library` 会自动拉取 |
| 想改密钥 | 先改 `config.yml` 并重启本站，再到 GitLab 编辑 Webhook，两处同步改 |
| GitLab 反复重试 | 本站接口通常在 1 秒内返回；若持续超时说明 GitLab 到本站网络不通 |
