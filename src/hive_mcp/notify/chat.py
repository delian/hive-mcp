"""Direct messages to people in Slack and Teams, for the same events that go out by email.

Both channels exist always and look at the stored integration settings at send time, so an admin
switching an integration on or off changes delivery within seconds. An integration that is off, or
a person the chat service cannot find, is skipped quietly (email still reaches them): only a
failing chat service raises, so the job runner retries it. Item content is never sent, only titles
and who asked, like every other notification.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import httpx

from hive_mcp.domain.errors import NotifyError
from hive_mcp.slack.handlers import mrkdwn
from hive_mcp.teams.connector import BotConnector

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from hive_mcp.integrations.config import IntegrationConfig
    from hive_mcp.ports.kv import KeyValueStore
    from hive_mcp.ports.notify import Notification

log = logging.getLogger("hive.notify")
LOOKUP = "https://slack.com/api/users.lookupByEmail"
POST = "https://slack.com/api/chat.postMessage"
SLACK_NS = "notify:slack:user"
TEAMS_REF_NS = "teams:ref"
CACHE_SECONDS = 24 * 3600


class SlackDmNotifier:
    def __init__(
        self,
        get_config: Callable[[], Awaitable[IntegrationConfig]],
        get_kv: Callable[[], KeyValueStore],
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._get_config, self._get_kv = get_config, get_kv
        self._http = http or httpx.AsyncClient(timeout=10.0)

    async def send(self, notification: Notification) -> None:
        config = await self._get_config()
        token = config.secrets.get("bot_token")
        if not (config.enabled and token):
            return
        text = f"*{mrkdwn(notification.subject)}*\n{mrkdwn(notification.text)}"
        for email in notification.recipients:
            user = await self._user_id(config, token, email)
            if user:
                await self._post(token, user, text)

    async def _slack(self, token: str, method: str, url: str, **kw: object) -> dict[str, object]:
        try:
            response = await self._http.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}"},
                **kw,  # type: ignore[arg-type]
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise NotifyError("Slack is unreachable") from exc
        return body if isinstance(body, dict) else {}

    async def _user_id(self, config: IntegrationConfig, token: str, email: str) -> str | None:
        key = f"{config.workspace}:{email}"
        cached = await self._get_kv().get(SLACK_NS, key)
        if cached is not None:
            return str(cached["id"]) or None
        body = await self._slack(token, "GET", LOOKUP, params={"email": email})
        if not body.get("ok") and body.get("error") != "users_not_found":
            raise NotifyError(f"Slack refused the lookup ({body.get('error')})")
        user = body.get("user")
        found = str(user["id"]) if isinstance(user, dict) and "id" in user else ""
        await self._get_kv().put(SLACK_NS, key, {"id": found}, ttl_seconds=CACHE_SECONDS)
        return found or None

    async def _post(self, token: str, user_id: str, text: str) -> None:
        body = await self._slack(token, "POST", POST, json={"channel": user_id, "text": text})
        if not body.get("ok"):
            raise NotifyError(f"Slack refused the message ({body.get('error')})")


class TeamsDmNotifier:
    """Proactive messages go to the personal chat a person already has with the bot (its address
    was remembered when they last wrote to it); people who never did are skipped."""

    def __init__(
        self,
        get_config: Callable[[], Awaitable[IntegrationConfig]],
        get_kv: Callable[[], KeyValueStore],
        make_connector: Callable[[IntegrationConfig], BotConnector] | None = None,
    ) -> None:
        self._get_config, self._get_kv = get_config, get_kv
        self._make = make_connector or (
            lambda c: BotConnector(c.app_id, c.secrets["app_password"], c.workspace)
        )
        self._built: tuple[IntegrationConfig, BotConnector] | None = None

    async def send(self, notification: Notification) -> None:
        config = await self._get_config()
        if not (config.enabled and config.secrets.get("app_password")):
            return
        if self._built is None or self._built[0] is not config:
            self._built = (config, self._make(config))
        connector = self._built[1]
        text = f"{notification.subject}\n\n{notification.text}"
        for email in notification.recipients:
            ref = await self._get_kv().get(TEAMS_REF_NS, email)
            if ref is None:
                log.info("no Teams chat known for a recipient; skipped")
                continue
            try:
                await connector.send(
                    str(ref["serviceUrl"]), str(ref["conversation"]), text, time.monotonic()
                )
            except Exception as exc:
                raise NotifyError("Teams delivery failed") from exc
