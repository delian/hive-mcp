"""The Teams message extension: "Search Hive" in the compose box, "Save to Hive" on a message, and
pasted links to notes unfurling into a card.

All of it arrives as `invoke` activities and is answered in the HTTP response, within five seconds.

Search results and unfurled links are shown to everyone in the conversation, so, as for channel
answers, only organisation-wide knowledge and the notes of the project the conversation is mapped
to are quoted; for anything else the card says only that a note exists and where to open it
(signing in decides who can read it). Saving a message makes a note: private to the person who
clicked, or, if they are in any projects, kept where they choose in a small dialog (the message is
held for ten minutes between opening the dialog and saving; saving twice is harmless because the
note is keyed to the message and the place).
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import TYPE_CHECKING, Any

from hive_mcp.domain.errors import HiveError
from hive_mcp.domain.item import Status
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.mcp_api.errors import user_message
from hive_mcp.services.models import ConsultRequest, RememberRequest, provenance
from hive_mcp.slack.handlers import audience_filter
from hive_mcp.teams.extension_cards import (
    PRIVATE,
    card,
    message,
    plain,
    result,
    save_dialog,
    task_message,
)
from hive_mcp.teams.handlers import MAX_NOTE, MAX_TITLE, NOT_LINKED, clean_text

if TYPE_CHECKING:
    from collections.abc import Callable

    from hive_mcp.domain.item import ItemView
    from hive_mcp.domain.principal import Principal
    from hive_mcp.teams.handlers import TeamsBot

SEARCH, SAVE = "searchHive", "saveMessage"
QUERY, FETCH, SUBMIT, LINK = (
    "composeExtension/query", "composeExtension/fetchTask", "composeExtension/submitAction",
    "composeExtension/queryLink",
)  # fmt: skip
HANDLED = (QUERY, FETCH, SUBMIT, LINK)
MAX_RESULTS = 10
MAX_QUERY = 200
SNIPPET = 300
SAVE_NS = "teams:save"
SAVE_TTL = 600
EXPIRED = "This dialog expired. Open Save to Hive on the message again."
NOTE_ID = re.compile(r"[0-9a-f]{32}")
NOBODY = ""  # pragma: no mutate - no author or project: the text is never shown or compared


class TeamsExtension:
    def __init__(self, bot: TeamsBot, tenant: str, project_for: dict[str, str]) -> None:
        self._bot, self._tenant, self._projects = bot, tenant, project_for

    async def invoke(self, activity: dict[str, Any]) -> dict[str, Any]:
        """The response body for an invoke activity ({} for the ones we do not handle)."""
        name = str(activity.get("name", ""))  # pragma: no mutate
        if name not in HANDLED:
            return {}
        reply = task_message if name in (FETCH, SUBMIT) else message
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
        return await self._route(name, reply, who, activity)

    async def _route(
        self, name: str, reply: Callable[[str], dict[str, Any]], who: Principal,
        activity: dict[str, Any],
    ) -> dict[str, Any]:  # fmt: skip
        value = activity.get("value") or {}
        if name == QUERY:
            return await self._search(who, activity, value)
        if name == LINK:
            return await self._unfurl(who, activity, str(value.get("url", "")))  # pragma: no mutate
        if value.get("commandId") != SAVE:
            return reply("Unknown command.")
        if name == FETCH:
            return await self._open_save(who, value.get("messagePayload") or {})
        return task_message(await self._submit_save(who, value.get("data") or {}))

    # ------------------------------------------------------------------ what may be quoted
    def _shared_project(self, who: Principal, activity: dict[str, Any]) -> str | None:
        conversation = str((activity.get("conversation") or {}).get("id", ""))
        project = self._projects.get(conversation)
        return project if project and project in who.projects else None

    def _link(self, item_id: str) -> str | None:
        base = self._bot.note_base
        return base + item_id if base else None

    # ------------------------------------------------------------------ search
    async def _search(
        self, who: Principal, activity: dict[str, Any], value: dict[str, Any]
    ) -> dict[str, Any]:
        params = {
            str(p.get("name")): str(p.get("value", "")) for p in value.get("parameters") or []
        }
        text = clean_text(params.get("query", ""))[:MAX_QUERY]
        if params.get("initialRun") == "true" or not text:
            return message("Type what you are looking for in the team's shared knowledge.")
        found = await self._bot.services().consult.consult(who, ConsultRequest(text, limit=20))
        matches = audience_filter(list(found.primary), self._shared_project(who, activity))
        if not matches[:MAX_RESULTS]:
            return message(f"Nothing shared with the whole team found for: {text}")
        cards = [
            card(m.title, m.provenance, plain(m.snippet)[:SNIPPET], self._link(m.item_id))
            for m in matches[:MAX_RESULTS]
        ]
        return result(cards)

    # ------------------------------------------------------------------ pasted links
    def _note_id(self, url: str) -> str | None:
        base = self._bot.note_base
        if not base or not url.startswith(base):
            return None
        rest = url[len(base) :].rstrip("/")
        return rest if NOTE_ID.fullmatch(rest) else None

    async def _unfurl(self, who: Principal, activity: dict[str, Any], url: str) -> dict[str, Any]:
        note_id = self._note_id(url)
        if note_id is None:
            return {}
        try:
            view: ItemView | None = await self._bot.services().knowledge.get(who, note_id)
        except HiveError:
            view = None  # not found and not visible look the same
        project = self._shared_project(who, activity)
        if view is None or not _shareable(view, project):
            generic = card("Hive note", "", "Open it in Hive to read it (sign-in required).", url)
            return result([generic], fresh=True)
        item = view.item
        where = provenance(item.scope, view.tier, NOBODY)  # pragma: no mutate - primary tier only
        return result([card(item.title, where, plain(item.body)[:SNIPPET], url)], fresh=True)

    # ------------------------------------------------------------------ saving a message
    async def _open_save(self, who: Principal, payload: dict[str, Any]) -> dict[str, Any]:
        text = _message_text(payload)
        if not text:
            return task_message("That message has no text to save.")
        if not who.projects:
            return task_message(await self._save(who, payload, None))
        token = secrets.token_urlsafe(16)
        held = {"user": who.id, "payload": _slim(payload)}
        await self._bot.kv().put(SAVE_NS, token, held, ttl_seconds=SAVE_TTL)
        return save_dialog(token, text, sorted(who.projects))

    async def _submit_save(self, who: Principal, data: dict[str, Any]) -> str:
        held = await self._bot.kv().get(SAVE_NS, str(data.get("token", "")))
        if held is None or held.get("user") != who.id:
            return EXPIRED
        choice = str(data.get("project", PRIVATE))
        if choice in ("", PRIVATE):
            return await self._save(who, held["payload"], None)
        if choice not in who.projects:
            return f"You are not in project {choice}."
        return await self._save(who, held["payload"], choice)

    async def _save(self, who: Principal, payload: dict[str, Any], project: str | None) -> str:
        body = _message_text(payload)  # callers have checked it is not empty
        link = str(payload.get("linkToMessage", ""))
        sender = str(((payload.get("from") or {}).get("user") or {}).get("displayName", ""))
        subject = clean_text(str(payload.get("subject") or "")) or body.splitlines()[0]
        if sender:
            body = f"{body}\n\n(from {sender} in Teams)"
        digest = hashlib.sha256(f"{link or body}\0{project or NOBODY}".encode()).hexdigest()[:32]
        saved = await self._bot.services().knowledge.remember(
            who,
            RememberRequest(
                title=subject[:MAX_TITLE],
                body=body,
                tags=("source/teams",),
                scope=Scope(ScopeLevel.PROJECT, project) if project else None,
                source=(link or f"teams:message:{payload.get('id', '')}")[:400],
                idempotency_key=f"teams-msg:{digest}:{who.id}",
            ),
        )
        verb = "Saved" if saved.created else "Already saved"
        where = f"in project {project}" if project else "as a private note"
        return f"{verb} {where}: {saved.item.title}"


def _message_text(payload: dict[str, Any]) -> str:
    return clean_text(str((payload.get("body") or {}).get("content", "")))[:MAX_NOTE]


def _slim(payload: dict[str, Any]) -> dict[str, Any]:
    """Only what a note is made from, so the held copy stays small."""
    sender = ((payload.get("from") or {}).get("user") or {}).get("displayName", "")
    return {
        "id": str(payload.get("id", "")), "subject": payload.get("subject"),
        "body": {"content": str((payload.get("body") or {}).get("content", ""))[: MAX_NOTE * 2]},
        "from": {"user": {"displayName": str(sender)}},
        "linkToMessage": str(payload.get("linkToMessage", "")),
    }  # fmt: skip


def _shareable(view: ItemView, project: str | None) -> bool:
    """May this note be quoted to everyone in the conversation (as `audience_filter` decides)?"""
    scope = view.item.scope
    return (
        view.tier.value == "primary"
        and view.item.status is Status.ACTIVE
        and (
            scope.level is ScopeLevel.ORG
            or bool(project and scope == Scope(ScopeLevel.PROJECT, project))
        )
    )
