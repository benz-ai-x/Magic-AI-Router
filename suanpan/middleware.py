"""FastAPI middleware for the Suanpan gateway: API key gate + body size limit.

Extracted from suanpan/main.py so the app factory only assembles; middleware
implementations have their own testable seam.
"""
from __future__ import annotations

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request

_AUTH_PUBLIC_PATHS = {"/health"}


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Optional API key gate for the gateway itself."""

    def __init__(self, app, *, api_key: str) -> None:
        super().__init__(app)
        self.api_key = api_key

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _AUTH_PUBLIC_PATHS:
            return await call_next(request)
        provided = request.headers.get("x-api-key")
        if not provided:
            auth = request.headers.get("authorization", "")
            if auth.startswith("Bearer "):
                provided = auth[len("Bearer "):]
        import hmac
        # 常量时间比较（#53）：对齐 config_server/panel 的密钥比较纪律。
        # provided 为 None（无凭证头）时 compare_digest 不接受混合类型，
        # 先短路——None 本就是 401。
        if provided is None or not hmac.compare_digest(provided, self.api_key):
            return JSONResponse(
                {"error": "missing or invalid api key"}, status_code=401)
        return await call_next(request)


class BodyLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, max_bytes: int):
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        # Fast path: Content-Length header present
        cl = request.headers.get("content-length")
        if cl:
            try:
                cl_val = int(cl)
            except ValueError:
                return JSONResponse(
                    {"error": "invalid Content-Length header"},
                    status_code=400,
                )
            if cl_val > self.max_bytes:
                return JSONResponse(
                    {"error": f"request body exceeds {self.max_bytes // 1048576}MB limit"},
                    status_code=413,
                )
        # Slow path: no Content-Length (chunked) — wrap receive to count bytes
        if cl is None and request.method in ("POST", "PUT", "PATCH"):
            received = 0
            original_receive = request.receive

            async def limited_receive():
                nonlocal received
                message = await original_receive()
                if message.get("type") == "http.request":
                    body = message.get("body", b"")
                    received += len(body)
                    if received > self.max_bytes:
                        raise _BodyTooLarge()
                return message

            request._receive = limited_receive
            try:
                return await call_next(request)
            except _BodyTooLarge:
                return JSONResponse(
                    {"error": f"request body exceeds {self.max_bytes // 1048576}MB limit"},
                    status_code=413,
                )
        return await call_next(request)


class _BodyTooLarge(Exception):
    pass
