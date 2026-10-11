from __future__ import annotations

import time
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logging import get_logger

log = get_logger("app.access")


class RequestContextMiddleware:
    """Assigns a request id, binds it to the log context, and emits one access log line.

    Implemented as pure ASGI middleware (not BaseHTTPMiddleware) to avoid its
    known issues with streaming and contextvars.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        request_id = headers.get("x-request-id") or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id, method=scope["method"], path=scope["path"]
        )
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message_headers = list(message.get("headers", []))
                message_headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = message_headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            log.info("request_completed", status_code=status_code, duration_ms=duration_ms)
            structlog.contextvars.clear_contextvars()
