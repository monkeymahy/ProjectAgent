"""Mock tForum：本地开发测试用，模拟 tForum 的 SSO。

跑在 8081。提供：
  GET /                              模拟 tForum 首页，点用户即跳 ProjectAgent 登录
  GET /api/v1/user/verifyToken?token=X   ProjectAgent 服务端调用，校验 token 返回用户

测试用户（token -> 用户）：
  u1     -> 张三   (member)   普通用户
  u2     -> 李四   (member)   普通用户
  admin  -> 管理员 (admin)    可改/删任意项目
  bad    -> 校验失败           模拟无效 token
  其它   -> 测试用户(10086)    方便快速登录

用法：
  1. 起本服务：python tests/mock_tforum.py
  2. 浏览器开 http://localhost:8081 ，点某个用户
     （等价于直接访问 http://localhost:8765/sso?token=u1）
  3. ProjectAgent 校验通过后会跳回首页并已登录该用户

测鉴权矩阵：
  - 用 u1 登录，提交项目 -> 项目归 u1
  - 换 u2 登录，去 u1 的详情页 -> 看不到编辑/删除按钮，PUT/DELETE 返回 403
  - 换 admin 登录 -> 能改/删任意项目
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PROJECTAGENT_URL = "http://localhost:8765"

USERS = {
    "u1": {"id": 1001, "username": "张三", "account": "zhangsan",
           "email": "zhangsan@example.com", "avatar": "", "role": "member"},
    "u2": {"id": 1002, "username": "李四", "account": "lisi",
           "email": "lisi@example.com", "avatar": "", "role": "member"},
    "admin": {"id": 1003, "username": "管理员", "account": "admin",
              "email": "admin@example.com", "avatar": "", "role": "admin"},
}
DEFAULT_USER = {"id": 10086, "username": "测试用户", "account": "tester",
                "email": "tester@example.com", "avatar": "", "role": "member"}


def verify(token: str):
    if token == "bad":
        return {"code": 1, "message": "token 无效"}
    data = USERS.get(token, DEFAULT_USER)
    return {"code": 0, "data": data}


HOME_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>Mock tForum</title>
<style>
body{background:#0d1117;color:#c9d1d9;font-family:-apple-system,"PingFang SC",sans-serif;
margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;}
.box{background:#161b22;border:1px solid #30363d;border-radius:14px;padding:32px;max-width:520px;width:90%;}
h1{margin:0 0 6px;font-size:22px;color:#fff;} p.sub{color:#8b949e;margin:0 0 22px;font-size:13px;}
.user{display:flex;align-items:center;gap:12px;padding:12px 14px;border:1px solid #30363d;
border-radius:10px;margin-bottom:10px;cursor:pointer;background:#0d1117;transition:border-color .15s;}
.user:hover{border-color:#58a6ff;}
.av{width:34px;height:34px;border-radius:50%;background:#58a6ff;color:#fff;font-weight:700;
display:flex;align-items:center;justify-content:center;}
.av.admin{background:#d29922;}
.name{font-size:15px;} .role{color:#8b949e;font-size:12px;margin-left:auto;
padding:2px 8px;border:1px solid #30363d;border-radius:999px;}
.hint{color:#8b949e;font-size:12px;margin-top:18px;line-height:1.6;}
code{background:#1f2428;padding:1px 6px;border-radius:4px;}
</style></head><body>
<div class="box">
<h1>Mock tForum</h1>
<p class="sub">点一个用户，会在新标签打开 ProjectAgent 并以该用户登录</p>
%USERS%
<div class="hint">
  对应直链：<br>
  <code>http://localhost:8765/sso?token=u1</code> ·
  <code>token=u2</code> · <code>token=admin</code><br>
  token=<code>bad</code> 模拟无效 token（登录失败页）。
</div>
</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)

        if u.path == "/api/v1/user/verifyToken":
            token = (qs.get("token") or [""])[0]
            body = json.dumps(verify(token), ensure_ascii=False)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return

        if u.path == "/" or u.path == "":
            rows = ""
            for tok, info in [("u1", USERS["u1"]), ("u2", USERS["u2"]), ("admin", USERS["admin"])]:
                url = f"{PROJECTAGENT_URL}/sso?token={tok}"
                role = "管理员" if info["role"] == "admin" else "普通用户"
                rows += (
                    f'<div class="user" onclick="window.open(\'{url}\',\'_blank\')">'
                    f'<div class="av {"admin" if info["role"]=="admin" else ""}">{info["username"][0]}</div>'
                    f'<div class="name">{info["username"]} <span style="color:#8b949e;font-size:12px;">{info["account"]}</span></div>'
                    f'<div class="role">{role}</div></div>'
                )
            html = HOME_HTML.replace("%USERS%", rows)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        # 只打印 verifyToken 调用，方便看 ProjectAgent 来校验了哪个 token
        if "verifyToken" in (args[0] if args else ""):
            print(f"[verifyToken] {args[0]}")


if __name__ == "__main__":
    print(f"Mock tForum on http://localhost:8081")
    print(f"登录入口页: http://localhost:8081")
    print(f"ProjectAgent: {PROJECTAGENT_URL}")
    print(f"测试用户: u1=张三(member)  u2=李四(member)  admin=管理员  bad=无效")
    ThreadingHTTPServer(("127.0.0.1", 8081), Handler).serve_forever()
