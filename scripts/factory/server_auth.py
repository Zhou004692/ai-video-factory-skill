"""Single-admin authenticated session boundary for the private server runtime."""
from __future__ import annotations

from dataclasses import dataclass
import base64
from hashlib import pbkdf2_hmac
import hmac
import json
import os
import secrets
import time
from typing import Mapping
from urllib.parse import urlsplit


COOKIE_NAME = "avf_session"
PUBLIC_ROUTES = {
    ("GET", "/healthz"),
    ("GET", "/readyz"),
    ("GET", "/login"),
    ("POST", "/api/auth/login"),
    ("GET", "/api/integrations/douyin/oauth/callback"),
}


class AuthRejected(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = int(status)


@dataclass(frozen=True)
class Session:
    expires_at: int
    csrf_token: str


@dataclass(frozen=True)
class IssuedSession:
    token: str
    csrf_token: str
    expires_at: int


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str, *, iterations: int = 310_000) -> str:
    if not str(password):
        raise ValueError("管理员密码不能为空")
    if iterations < 10_000:
        raise ValueError("密码哈希迭代次数过低")
    salt = os.urandom(16)
    digest = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = str(encoded).split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = pbkdf2_hmac(
            "sha256", str(password).encode("utf-8"), _unb64(salt), int(iterations)
        )
        return hmac.compare_digest(actual, _unb64(expected))
    except (TypeError, ValueError):
        return False


class ServerSessionManager:
    def __init__(
        self,
        *,
        session_secret: str,
        password_hash: str,
        public_base_url: str,
        session_ttl_sec: int = 12 * 60 * 60,
    ) -> None:
        if len(session_secret) < 32:
            raise ValueError("会话密钥至少需要 32 位")
        if not str(password_hash).startswith("pbkdf2_sha256$"):
            raise ValueError("管理员密码必须使用 pbkdf2_sha256 哈希")
        parsed = urlsplit(public_base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("服务端公开地址必须使用 HTTPS")
        self._secret = session_secret.encode("utf-8")
        self._password_hash = password_hash
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.host = parsed.netloc.casefold()
        self.session_ttl_sec = max(60, int(session_ttl_sec))

    def _sign(self, payload: bytes) -> str:
        return _b64(hmac.new(self._secret, payload, "sha256").digest())

    def login(self, password: str, *, now: int | None = None) -> IssuedSession:
        if not verify_password(password, self._password_hash):
            raise ValueError("用户名或密码错误")
        current = int(time.time() if now is None else now)
        csrf = secrets.token_urlsafe(24)
        payload = json.dumps(
            {"exp": current + self.session_ttl_sec, "csrf": csrf},
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
        encoded = _b64(payload)
        token = f"{encoded}.{self._sign(payload)}"
        return IssuedSession(token, csrf, current + self.session_ttl_sec)

    def authenticate(
        self, cookie_header: str | None, *, now: int | None = None
    ) -> Session | None:
        cookies = {}
        for item in str(cookie_header or "").split(";"):
            if "=" in item:
                key, value = item.strip().split("=", 1)
                cookies[key] = value
        token = cookies.get(COOKIE_NAME, "")
        try:
            encoded, signature = token.split(".", 1)
            payload = _unb64(encoded)
            if not hmac.compare_digest(signature, self._sign(payload)):
                return None
            parsed = json.loads(payload.decode("utf-8"))
            expires = int(parsed["exp"])
            csrf = str(parsed["csrf"])
        except (ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        current = int(time.time() if now is None else now)
        if expires < current or not csrf:
            return None
        return Session(expires, csrf)

    def authorize(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        *,
        now: int | None = None,
    ) -> Session | None:
        normalized = {str(key).casefold(): str(value) for key, value in headers.items()}
        if normalized.get("host", "").casefold() != self.host:
            raise AuthRejected(403, "请求 Host 不可信")
        method = method.upper()
        public = (method, path) in PUBLIC_ROUTES
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            if normalized.get("origin") != self.origin:
                raise AuthRejected(403, "请求来源不可信")
        if public:
            return None
        session = self.authenticate(normalized.get("cookie"), now=now)
        if session is None:
            raise AuthRejected(401, "请先登录")
        if method in {"POST", "PUT", "PATCH", "DELETE"} and not hmac.compare_digest(
            normalized.get("x-csrf-token", ""), session.csrf_token
        ):
            raise AuthRejected(403, "CSRF 校验失败")
        return session
