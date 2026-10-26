"""Request id, one structured access-log line and HTTP metrics per request (pure ASGI)."""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import TYPE_CHECKING

from hive_mcp.ops.logs import request_id_var

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from hive_mcp.ops.metrics import Metrics

log = logging.getLogger("hive.access")
VALID_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
OAUTH = ("/oauth", "/authorize", "/token", "/register", "/revoke", "/.well-known")
QUIET = {"health", "metrics"}  # scraped constantly: logged at debug only


def route_group(path: str, mcp_path: str) -> str:
    if path.startswith(mcp_path):
        return "mcp"
    if path.startswith(OAUTH):
        return "oauth"
    if path.startswith("/admin"):
        return "admin"
    if path in ("/healthz", "/readyz"):
        return "health"
    return "metrics" if path == "/metrics" else "other"


class ObserveMiddleware:
    def __init__(self, app: ASGIApp, metrics: Metrics, mcp_path: str = "/mcp") -> None:
        self.app, self.metrics, self.mcp_path = app, metrics, mcp_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers", [])).get(b"x-request-id", b"").decode("latin-1")
        rid = incoming if VALID_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(rid)
        status = 500
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [*message.get("headers", []), (b"x-request-id", rid.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            seconds = time.perf_counter() - started
            group = route_group(scope["path"], self.mcp_path)
            self.metrics.observe_request(group, scope["method"], status, seconds)
            client = scope.get("client")
            log.log(
                logging.DEBUG if group in QUIET else logging.INFO,
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "ms": round(seconds * 1000, 1),
                    "group": group,
                    "client": client[0] if client else None,
                },
            )
            request_id_var.reset(token)
