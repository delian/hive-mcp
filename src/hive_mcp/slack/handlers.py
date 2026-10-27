"""What the Slack commands do. Replies are always ephemeral (only the asker sees them): search
results and saved notes are as private as in any other front door."""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING

import httpx

from hive_mcp.domain.errors import HiveError
from hive_mcp.mcp_api.errors import user_message
from hive_mcp.services.models import ConsultRequest, RememberRequest

if TYPE_CHECKING:
    from collections.abc import Callable

    from hive_mcp.bootstrap import Services
    from hive_mcp.domain.principal import Principal
    from hive_mcp.slack.identity import SlackIdentity

log = logging.getLogger("hive.slack")
RESULTS = 5
MAX_TITLE = 80
MAX_NOTE = 20_000
RESPONSE_HOSTS = ("https://hooks.slack.com/",)
HELP = (
    "*Hive*: the team's shared memory.\n"
    "`/hive <question>`  search what the team knows\n"
    "`/hive save <text>`  keep a note (private to you; share it later from your agent)\n"
    "Message shortcut *Save to Hive*: keep any message as a private note."
)
NOT_LINKED = (
    "I could not match your Slack account to a Hive user. Ask an admin to add your work email "
    "(`hive user add`), then try again."
)


def mrkdwn(text: str) -> str:
    """Slack's three escapes: stops stored text from creating mentions, links or formatting
    tricks (`<!channel>`, `<http://evil|label>`) in a reply."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def ephemeral(text: str) -> dict[str, str]:
    return {"response_type": "ephemeral", "text": text}


class SlackBot:
    def __init__(
        self,
        get_services: Callable[[], Services],
        identity: SlackIdentity,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._services, self._identity = get_services, identity
        self._http = http or httpx.AsyncClient(timeout=5.0)

    async def command(self, form: dict[str, str]) -> dict[str, str]:
        who = await self._identity.resolve(form.get("team_id", ""), form.get("user_id", ""))
        if who is None:
            return ephemeral(NOT_LINKED)
        text = form.get("text", "").strip()
        try:
            if text in ("", "help"):
                return ephemeral(HELP)
            if text.split(None, 1)[0].lower() == "save":
                body = text.split(None, 1)[1] if " " in text else ""
                if not body.strip():
                    return ephemeral("Nothing to save: `/hive save <text>`")
                return ephemeral(await self._save(who, body, f"slack:{form.get('team_id')}:cmd"))
            return ephemeral(await self._search(who, text))
        except HiveError as exc:
            return ephemeral(f"Sorry: {mrkdwn(user_message(exc))}")

    async def shortcut(self, payload: dict[str, object]) -> str:
        """Save the message the shortcut was used on; returns the confirmation text."""
        user, team = _dict(payload.get("user")), _dict(payload.get("team"))
        who = await self._identity.resolve(str(team.get("id", "")), str(user.get("id", "")))
        if who is None:
            return NOT_LINKED
        message, channel = _dict(payload.get("message")), _dict(payload.get("channel"))
        text = html.unescape(str(message.get("text", ""))).strip()
        if not text:
            return "That message has no text to save."
        origin = f"slack:{team.get('id')}/{channel.get('id')}/{message.get('ts')}"
        try:
            return await self._save(
                who, text, origin, note=f"From Slack #{channel.get('name', '')}"
            )
        except HiveError as exc:
            return f"Sorry: {mrkdwn(user_message(exc))}"

    async def _search(self, who: Principal, query: str) -> str:
        result = await self._services().consult.consult(who, ConsultRequest(query, limit=RESULTS))
        matches = [*result.primary, *result.referenced][:RESULTS]
        if not matches:
            return f"Nothing found for *{mrkdwn(query)}*."
        lines = [f"*{len(matches)} result(s)* for _{mrkdwn(query)}_"]
        for m in matches:
            snippet = mrkdwn(m.snippet.replace("\n", " "))[:200]
            lines.append(f"*{mrkdwn(m.title)}*  _{mrkdwn(m.provenance)}_\n{snippet}\n`{m.item_id}`")
        return "\n\n".join(lines)

    async def _save(self, who: Principal, body: str, origin: str, note: str = "") -> str:
        body = body[:MAX_NOTE]
        first = body.strip().splitlines()[0][:MAX_TITLE]
        saved = await self._services().knowledge.remember(
            who,
            RememberRequest(
                title=first or "Slack note",
                body=f"{note}\n\n{body}".strip() if note else body,
                tags=("source/slack",),
                source=origin[:400],
                idempotency_key=f"slack:{origin}:{who.id}"[:200],
            ),
        )
        flagged = (
            " (flagged as possibly unsafe text)" if saved.item.tags & {"flag/injection"} else ""
        )
        verb = "Saved" if saved.created else "Already saved"
        return (
            f"{verb} as a private note: *{mrkdwn(saved.item.title)}*{flagged}\n`{saved.item.id}`"
            "  Share it with your project from your agent (`kb_share`)."
        )

    async def reply(self, response_url: str, text: str) -> None:
        """Post a follow-up through the `response_url` Slack supplied (only to Slack's own host)."""
        if not response_url.startswith(RESPONSE_HOSTS):
            log.warning("slack response_url refused")
            return
        try:
            await self._http.post(response_url, json=ephemeral(text))
        except httpx.HTTPError:
            log.warning("slack follow-up failed")


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}
