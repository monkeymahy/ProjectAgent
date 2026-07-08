"""Mock tForum：仅用于本地测试 SSO。
跑在 8081，对 /api/v1/user/verifyToken 返回一个假用户。
token == 'bad' 时返回失败，模拟无效 token。
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if "/api/v1/user/verifyToken" in self.path:
            token = self.path.split("token=")[-1] if "token=" in self.path else ""
            if token == "bad":
                body = json.dumps({"code": 1, "message": "token 无效"})
            else:
                body = json.dumps({
                    "code": 0,
                    "data": {
                        "id": 10086,
                        "username": "测试用户",
                        "account": "tester",
                        "email": "tester@example.com",
                        "avatar": "",
                        "role": "member",
                    },
                })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8081), Handler).serve_forever()
