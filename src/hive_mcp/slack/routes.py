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
    from collections.abc import Callable

    from starlette.requests import Request

    from hive_mcp.ports.kv import KeyValueStore
    from hive_mcp.slack.handlers import SlackBot

SEEN_NS = "slack:seen"
MAX_BODY = 64 * 1024


class SlackRoutes:
    def __init__(self, bot: SlackBot, secret: str, get_kv: Callable[[], KeyValueStore]) -> None:
        self._bot, self._secret, self._get_kv = bot, secret, get_kv

    async def _checked_body(self, request: Request) -> bytes | Response:
        body = await request.body()
        h = request.headers
        signature = h.get("x-slack-signature")
        if len(body) > MAX_BODY or not verify(
            self._secret, h.get("x-slack-request-timestamp"), signature, body
        ):
            return Response("invalid signature", status_code=401)
        fresh = await self._get_kv().put_if_absent(
            SEEN_NS, str(signature), {"seen": True}, ttl_seconds=WINDOW_SECONDS * 2
        )
        return body if fresh else Response("replayed request", status_code=401)

    async def commands(self, request: Request) -> Response:
        body = await self._checked_body(request)
        if isinstance(body, Response):
            return body
        form = dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
        return JSONResponse(await self._bot.command(form))

    async def interactions(self, request: Request) -> Response:
        body = await self._checked_body(request)
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
        text = await self._bot.shortcut(payload)
        await self._bot.reply(str(payload.get("response_url", "")), text)
        return Response(status_code=200)  # Slack wants an empty acknowledgement

    def routes(self) -> list[Route]:
        return [
            Route("/slack/commands", self.commands, methods=["POST"]),
            Route("/slack/interactions", self.interactions, methods=["POST"]),
        ]
