"""The Teams route: POST /teams/messages. Authenticated by the Bot Framework's signed token."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from hive_mcp.security.oauth.upstream import UpstreamError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request

    from hive_mcp.integrations.config import IntegrationConfig
    from hive_mcp.ports.kv import KeyValueStore
    from hive_mcp.teams.handlers import TeamsBot
    from hive_mcp.teams.verify import BotFrameworkVerifier

MAX_BODY = 256 * 1024
SEEN_NS = "teams:activity"


class TeamsRoutes:
    """Always mounted; whether it answers is decided per request by the stored settings."""

    def __init__(
        self,
        get_config: Callable[[], Awaitable[IntegrationConfig]],
        make: Callable[[IntegrationConfig], tuple[BotFrameworkVerifier, TeamsBot]],
        get_kv: Callable[[], KeyValueStore],
    ) -> None:
        self._get_config, self._make, self._get_kv = get_config, make, get_kv
        self._built: tuple[IntegrationConfig, BotFrameworkVerifier, TeamsBot] | None = None

    async def messages(self, request: Request) -> Response:
        config = await self._get_config()
        if not config.enabled:
            return Response(status_code=404)
        if self._built is None or self._built[0] != config:
            self._built = (config, *self._make(config))
        _, verifier, bot = self._built
        body = await request.body()
        if len(body) > MAX_BODY:
            return Response("too large", status_code=413)
        try:
            activity = json.loads(body)
            if not isinstance(activity, dict):
                raise ValueError
        except (ValueError, RecursionError):  # deeply nested JSON raises RecursionError
            return Response("bad activity", status_code=400)
        try:
            await verifier.verify(
                request.headers.get("authorization"), str(activity.get("serviceUrl", ""))
            )
        except UpstreamError:
            return Response("invalid token", status_code=401)
        if activity.get("type") == "invoke":  # answered in the response; a retry just asks again
            return JSONResponse(await bot.invoke(activity))
        activity_id = str(activity.get("id", ""))
        fresh = (
            await self._get_kv().put_if_absent(  # Teams redelivers slow activities: once
                SEEN_NS, activity_id, {"seen": True}, ttl_seconds=3600
            )
            if activity_id
            else True
        )
        if fresh:
            await bot.handle(activity)
        return Response(status_code=200)

    def routes(self) -> list[Route]:
        return [Route("/teams/messages", self.messages, methods=["POST"])]
