"""ASGI application factory: MCP endpoint plus ops routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.config import Settings
from hive_mcp.mcp_api.server import build_mcp
from hive_mcp.ops.health import health_routes
from hive_mcp.ops.state import Lifecycle

if TYPE_CHECKING:
    from starlette.applications import Starlette


def create_app(settings: Settings | None = None, lifecycle: Lifecycle | None = None) -> Starlette:
    settings = settings or Settings()
    lifecycle = lifecycle or Lifecycle()
    mcp = build_mcp()
    app = mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        stateless_http=True,
        json_response=True,
        host=settings.host,
    )
    app.router.routes.extend(health_routes(lifecycle))
    app.state.lifecycle = lifecycle
    return app
