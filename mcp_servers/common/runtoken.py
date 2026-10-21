"""실행 토큰 (명세 2장).

평가 실행마다 business-mcp가 발급하고 두 MCP가 같은 비밀키(RUN_TOKEN_SECRET)로 검증한다.
형식: base64url(JSON claims) + "." + base64url(HMAC-SHA256(claims)).
도구 입력으로 company_id·user_id를 받지 않으므로 회사·업무·스냅샷 범위는 전부 여기서 나온다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


class TokenError(Exception):
    pass


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue(claims: dict, secret: str, ttl_sec: int = 3600) -> str:
    now = int(time.time())
    body = {**claims, "iat": now, "exp": now + ttl_sec}
    payload = _b64e(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify(token: str, secret: str) -> dict:
    try:
        payload, sig = token.strip().split(".")
    except ValueError:
        raise TokenError("실행 토큰 형식이 올바르지 않습니다")
    expected = _b64e(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise TokenError("실행 토큰 서명이 올바르지 않습니다")
    claims = json.loads(_b64d(payload))
    if claims.get("exp", 0) < time.time():
        raise TokenError("실행 토큰이 만료되었습니다")
    return claims
