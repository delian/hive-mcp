"""Liveness and readiness routes. Readiness fails while draining, or while the database is gone."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from hive_mcp import __version__

if TYPE_CHECKING:
    from starlette.requests import Request

    from hive_mcp.ops.state import Lifecycle


def health_routes(lifecycle: Lifecycle) -> list[Route]:
    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok", "version": __version__})

    async def readyz(_: Request) -> Response:
        if lifecycle.draining:
            return JSONResponse({"status": "draining"}, status_code=503)
        if not lifecycle.ready:
            return JSONResponse({"status": "starting"}, status_code=503)
        if lifecycle.probe is not None and not await lifecycle.probe():
            return JSONResponse({"status": "storage unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    return [Route("/healthz", healthz), Route("/readyz", readyz)]
