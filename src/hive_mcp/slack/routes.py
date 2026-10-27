"""HTTP routes Slack calls: /slack/commands (slash command) and /slack/interactions (shortcut).

Both are authenticated only by the signature of the request (no bearer token exists here), so the
signature check comes first, before the body is parsed, and a verified request is accepted once.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from hive_mcp.slack.verify import WINDOW_SECONDS, verify

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request

    from hive_mcp.integrations.config import IntegrationConfig
    from hive_mcp.ports.kv import KeyValueStore
    from hive_mcp.slack.handlers import SlackBot

SEEN_NS = "slack:seen"
EVENT_NS = "slack:event"
MAX_BODY = 64 * 1024


def wanted_mention(
    config: IntegrationConfig, payload: dict[str, object]
) -> dict[str, object] | None:
    """The event to answer, or None: mentions must be allowed, from our workspace, and from a
    person (never a bot or ourselves, which would loop)."""
    event = payload.get("event")
    if (
        payload.get("type") != "event_callback"
        or not config.allow_mentions
        or payload.get("team_id") != config.workspace
        or not isinstance(event, dict)
        or event.get("type") != "app_mention"
    ):
        return None
    return None if event.get("bot_id") or event.get("subtype") else event


class SlackRoutes:
    """The routes always exist; whether they answer is decided per request by the stored
    settings, so an admin can switch Slack on, off or over to another workspace live."""

    def __init__(
        self,
        get_config: Callable[[], Awaitable[IntegrationConfig]],
        make_bot: Callable[[IntegrationConfig], SlackBot],
        get_kv: Callable[[], KeyValueStore],
    ) -> None:
        self._get_config, self._make_bot, self._get_kv = get_config, make_bot, get_kv
        self._bot: tuple[IntegrationConfig, SlackBot] | None = None

    async def _active(self) -> tuple[IntegrationConfig, SlackBot] | None:
        config = await self._get_config()
        if not config.enabled:
            return None
        if self._bot is None or self._bot[0] != config:
            self._bot = (config, self._make_bot(config))
        return self._bot

    async def _checked_body(self, request: Request, secret: str) -> bytes | Response:
        body = await request.body()
        h = request.headers
        signature = h.get("x-slack-signature")
        if len(body) > MAX_BODY or not verify(
            secret, h.get("x-slack-request-timestamp"), signature, body
        ):
            return Response("invalid signature", status_code=401)
        fresh = await self._get_kv().put_if_absent(
            SEEN_NS, str(signature), {"seen": True}, ttl_seconds=WINDOW_SECONDS * 2
        )
        return body if fresh else Response("replayed request", status_code=401)

    async def commands(self, request: Request) -> Response:
        active = await self._active()
        if active is None:
            return Response(status_code=404)
        config, bot = active
        body = await self._checked_body(request, config.secrets["signing_secret"])
        if isinstance(body, Response):
            return body
        form = dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
        return JSONResponse(await bot.command(form))

    async def interactions(self, request: Request) -> Response:
        active = await self._active()
        if active is None:
            return Response(status_code=404)
        config, bot = active
        body = await self._checked_body(request, config.secrets["signing_secret"])
        if isinstance(body, Response):
            return body
        form = dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
        try:
            payload = json.loads(form.get("payload", ""))
        except ValueError:
            return Response("bad payload", status_code=400)
        if not isinstance(payload, dict) or payload.get("type") != "message_action":
            return Response(status_code=200)  # other interaction kinds are not used
        if payload.get("callback_id") != "hive_save":
            return Response(status_code=200)
        text = await bot.shortcut(payload)
        await bot.reply(str(payload.get("response_url", "")), text)
        return Response(status_code=200)  # Slack wants an empty acknowledgement

    async def events(self, request: Request) -> Response:
        """Events API: the one-time URL check, and @-mentions (only when an admin allowed them)."""
        active = await self._active()
        if active is None:
            return Response(status_code=404)
        config, bot = active
        body = await self._checked_body(request, config.secrets["signing_secret"])
        if isinstance(body, Response):
            return body
        try:
            payload = json.loads(body)
        except ValueError:
            return Response("bad payload", status_code=400)
        if not isinstance(payload, dict):
            return Response("bad payload", status_code=400)
        if payload.get("type") == "url_verification":
            return JSONResponse({"challenge": str(payload.get("challenge", ""))})
        event = wanted_mention(config, payload)
        if event is not None:
            fresh = await self._get_kv().put_if_absent(  # Slack retries slow deliveries: once
                EVENT_NS, str(payload.get("event_id", "")), {"seen": True}, ttl_seconds=3600
            )
            if fresh:
                await bot.mention(event, str(payload["team_id"]))
        return Response(status_code=200)

    def routes(self) -> list[Route]:
        return [
            Route("/slack/commands", self.commands, methods=["POST"]),
            Route("/slack/interactions", self.interactions, methods=["POST"]),
            Route("/slack/events", self.events, methods=["POST"]),
        ]
