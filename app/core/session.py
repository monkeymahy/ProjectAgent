"""SSO 会话：基于 itsdangerous 签名的 cookie。

流程：/sso 校验 tForum token 通过后，把 user_id 签进 cookie；
后续请求由 current_user 依赖解开 cookie → 查 users 表 → 注入用户。
"""
from __future__ import annotations

from typing import Optional

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from app.core.config import settings
from app.models.models import get_user

COOKIE_NAME = "pa_session"
_serializer = URLSafeTimedSerializer(settings.sso_cookie_secret, salt="pa-sso")


def sign_session(user_id: int) -> str:
    """把 tForum user_id 签成 cookie 值。"""
    return _serializer.dumps({"uid": int(user_id)})


def verify_session(token: str) -> Optional[dict]:
    """解开 cookie，返回用户 dict；过期或非法返回 None。"""
    try:
        data = _serializer.loads(token, max_age=settings.sso_session_ttl)
    except (BadSignature, SignatureExpired):
        return None
    uid = data.get("uid")
    if uid is None:
        return None
    return get_user(int(uid))
