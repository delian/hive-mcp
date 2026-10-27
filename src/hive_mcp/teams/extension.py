"""The Teams message extension: "Search Hive" in the compose box, "Save to Hive" on a message.

Both arrive as `invoke` activities and are answered in the HTTP response, within five seconds.

Search results are inserted into a conversation by the person who picks them, in front of
everyone there, so, as for channel answers, only organisation-wide knowledge and the notes of the
project the conversation is mapped to are offered; private and project notes stay with the bot's
personal chat. Saving a message makes a note that is private to the person who clicked (it is
saved while the dialog opens and the dialog says so: no form, one click; saving twice is
harmless because the note is keyed to the message).
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any

from hive_mcp.domain.errors import HiveError
from hive_mcp.mcp_api.errors import user_message
from hive_mcp.services.models import ConsultRequest, RememberRequest
from hive_mcp.slack.handlers import audience_filter
from hive_mcp.teams.handlers import MAX_NOTE, MAX_TITLE, NOT_LINKED, clean_text

if TYPE_CHECKING:
    from collections.abc import Callable

    from hive_mcp.domain.principal import Principal
    from hive_mcp.teams.handlers import TeamsBot

SEARCH, SAVE = "searchHive", "saveMessage"
MAX_RESULTS = 10
MAX_QUERY = 200
SNIPPET = 300
MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+\-.!|~<>])")


def plain(text: str) -> str:
    """Stored text as a card shows it, escaped: no link, mention or formatting can come out."""
    return MARKDOWN.sub(r"\\\1", " ".join(text.split()))


def message(text: str) -> dict[str, Any]:
    return {"composeExtension": {"type": "message", "text": text}}


def task_message(text: str) -> dict[str, Any]:
    return {"task": {"type": "message", "value": text}}


def card(title: str, subtitle: str, text: str) -> dict[str, Any]:
    content = {"title": plain(title)[:200], "subtitle": plain(subtitle)[:100], "text": text}
    return {
        "contentType": "application/vnd.microsoft.card.hero",
        "content": content,
        "preview": {"contentType": "application/vnd.microsoft.card.hero", "content": content},
    }


class TeamsExtension:
    def __init__(self, bot: TeamsBot, tenant: str, project_for: dict[str, str]) -> None:
        self._bot, self._tenant, self._projects = bot, tenant, project_for

    async def invoke(self, activity: dict[str, Any]) -> dict[str, Any]:
        """The response body for an invoke activity ({} for the ones we do not handle)."""
        name = str(activity.get("name", ""))  # pragma: no mutate
        if name not in ("composeExtension/query", "composeExtension/fetchTask"):
            return {}
        reply = message if name == "composeExtension/query" else task_message
        try:
            return await self._dispatch(name, reply, activity)
        except HiveError as exc:
            return reply(f"Sorry: {user_message(exc)}")

    async def _dispatch(
        self, name: str, reply: Callable[[str], dict[str, Any]], activity: dict[str, Any]
    ) -> dict[str, Any]:
        if ((activity.get("channelData") or {}).get("tenant") or {}).get("id") != self._tenant:
            return reply("This app is not available to your organisation.")
        who = await self._bot.resolve(activity)
        if who is None:
            return reply(NOT_LINKED)
        value = activity.get("value") or {}
        if name == "composeExtension/query":
            return await self._search(who, activity, value)
        if value.get("commandId") == SAVE:
            return task_message(await self._save(who, value.get("messagePayload") or {}))
        return reply("Unknown command.")

    async def _search(
        self, who: Principal, activity: dict[str, Any], value: dict[str, Any]
    ) -> dict[str, Any]:
        params = {
            str(p.get("name")): str(p.get("value", "")) for p in value.get("parameters") or []
        }
        text = clean_text(params.get("query", ""))[:MAX_QUERY]
        if params.get("initialRun") == "true" or not text:
            return message("Type what you are looking for in the team's shared knowledge.")
        conversation = str((activity.get("conversation") or {}).get("id", ""))
        project = self._projects.get(conversation)
        result = await self._bot.services().consult.consult(who, ConsultRequest(text, limit=20))
        matches = audience_filter(
            list(result.primary), project if project and project in who.projects else None
        )[:MAX_RESULTS]
        if not matches:
            return message(f"Nothing shared with the whole team found for: {text}")
        cards = [card(m.title, m.provenance, plain(m.snippet)[:SNIPPET]) for m in matches]
        return {
            "composeExtension": {"type": "result", "attachmentLayout": "list", "attachments": cards}
        }

    async def _save(self, who: Principal, payload: dict[str, Any]) -> str:
        body_part = payload.get("body") or {}
        body = clean_text(str(body_part.get("content", "")))[:MAX_NOTE]
        if not body:
            return "That message has no text to save."
        link = str(payload.get("linkToMessage", ""))
        sender = str(((payload.get("from") or {}).get("user") or {}).get("displayName", ""))
        subject = clean_text(str(payload.get("subject") or "")) or body.splitlines()[0]
        if sender:
            body = f"{body}\n\n(from {sender} in Teams)"
        digest = hashlib.sha256((link or body).encode()).hexdigest()[:32]
        saved = await self._bot.services().knowledge.remember(
            who,
            RememberRequest(
                title=subject[:MAX_TITLE],
                body=body,
                tags=("source/teams",),
                source=(link or f"teams:message:{payload.get('id', '')}")[:400],
                idempotency_key=f"teams-msg:{digest}:{who.id}",
            ),
        )
        verb = "Saved" if saved.created else "Already saved"
        return f"{verb} as a private note: {saved.item.title}"
