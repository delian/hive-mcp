"""What the Slack commands do. Replies are always ephemeral (only the asker sees them): search
results and saved notes are as private as in any other front door."""

from __future__ import annotations

import hashlib
import html
import logging
import re
from typing import TYPE_CHECKING

import httpx

from hive_mcp.domain.errors import HiveError
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.mcp_api.errors import user_message
from hive_mcp.services.models import ConsultRequest, RememberRequest

if TYPE_CHECKING:
    from collections.abc import Callable

    from hive_mcp.bootstrap import Services
    from hive_mcp.domain.principal import Principal
    from hive_mcp.services.models import Match
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


MENTION = re.compile(r"<@[A-Z0-9]+(?:\|[^>]*)?>")
POST_MESSAGE = "https://slack.com/api/chat.postMessage"


def audience_filter(matches: list[Match], channel_project: str | None) -> list[Match]:
    """What may be said in front of a whole channel: org-wide knowledge, plus the notes of the
    project the channel is mapped to. Private and other shared notes stay in the private `/hive`."""
    return [
        m
        for m in matches
        if m.tier.value == "primary"
        and (
            m.scope.level is ScopeLevel.ORG
            or (channel_project and m.scope == Scope(ScopeLevel.PROJECT, channel_project))
        )
    ]


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
        channel_projects: dict[str, str] | None = None,
        bot_token: str | None = None,
    ) -> None:
        self._services, self._identity = get_services, identity
        self._channel_projects = channel_projects or {}
        self._bot_token = bot_token
        self._http = http or httpx.AsyncClient(timeout=5.0)

    async def mention(self, event: dict[str, object], team_id: str) -> None:
        """Answer an @-mention in a thread, quoting only what the whole channel may see."""
        if not self._bot_token:
            return
        text = html.unescape(MENTION.sub("", str(event.get("text", "")))).strip()
        channel = str(event.get("channel", ""))
        thread = str(event.get("thread_ts") or event.get("ts") or "")
        who = await self._identity.resolve(team_id, str(event.get("user", "")))
        if who is None:
            await self.post(channel, thread, NOT_LINKED)
        elif not text:
            await self.post(channel, thread, "Ask me something: `@hive how do we deploy?`")
        else:
            await self.post(channel, thread, await self._public_answer(who, channel, text))

    async def _public_answer(self, who: Principal, channel: str, text: str) -> str:
        try:
            result = await self._services().consult.consult(who, ConsultRequest(text, limit=10))
        except HiveError as exc:
            return f"Sorry: {mrkdwn(user_message(exc))}"
        project = self._channel_projects.get(channel)
        audience = project if project in who.projects else None
        shown = audience_filter(list(result.primary), audience)[:RESULTS]
        if not shown:
            hint = " I only quote shared knowledge in channels." if result.primary else ""
            return (
                f"Nothing to share here for *{mrkdwn(text)}*.{hint} "
                f"`/hive {mrkdwn(text)}` answers privately."
            )
        lines = [f"*{len(shown)} result(s)* for _{mrkdwn(text)}_"]
        for m in shown:
            snippet = mrkdwn(m.snippet.replace("\n", " "))[:200]
            lines.append(f"*{mrkdwn(m.title)}*  _{mrkdwn(m.provenance)}_\n{snippet}")
        return "\n\n".join(lines)

    async def post(self, channel: str, thread_ts: str, text: str) -> None:
        try:
            await self._http.post(
                POST_MESSAGE,
                headers={"Authorization": f"Bearer {self._bot_token}"},
                json={
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "text": text,
                    "unfurl_links": False,
                },
            )
        except httpx.HTTPError:
            log.warning("slack chat.postMessage failed")

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
                origin = f"slack:{form.get('team_id')}:{form.get('channel_id')}:cmd"
                project = self._project_for(who, form.get("channel_id", ""))
                return ephemeral(await self._save(who, body, origin, project=project))
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
                who,
                text,
                origin,
                note=f"From Slack #{channel.get('name', '')}",
                project=self._project_for(who, str(channel.get("id", ""))),
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

    def _project_for(self, who: Principal, channel_id: str) -> str | None:
        """The project this channel is mapped to, if the person is a member of it (an admin chose
        the mapping; membership is still checked, so a mapping never widens anyone's access)."""
        project = self._channel_projects.get(channel_id)
        return project if project and project in who.projects else None

    async def _save(
        self, who: Principal, body: str, origin: str, note: str = "", project: str | None = None
    ) -> str:
        body = body[:MAX_NOTE]
        # the key names the place and the text: one key per place made a second, different note
        # from the same channel fail as an idempotency conflict
        digest = hashlib.sha256(f"{project}\0{body}".encode()).hexdigest()[:24]
        first = body.strip().splitlines()[0][:MAX_TITLE]
        saved = await self._services().knowledge.remember(
            who,
            RememberRequest(
                title=first or "Slack note",
                body=f"{note}\n\n{body}".strip() if note else body,
                tags=("source/slack",),
                scope=Scope(ScopeLevel.PROJECT, project) if project else None,
                source=origin[:400],
                idempotency_key=f"slack:{digest}:{origin}:{who.id}"[:200],
            ),
        )
        flagged = (
            " (flagged as possibly unsafe text)" if saved.item.tags & {"flag/injection"} else ""
        )
        verb = "Saved" if saved.created else "Already saved"
        if project:
            where = f"to project *{mrkdwn(project)}* (visible to its members)"
            tail = ""
        else:
            where = "as a private note"
            tail = "  Share it with your project from your agent (`kb_share`)."
        return f"{verb} {where}: *{mrkdwn(saved.item.title)}*{flagged}\n`{saved.item.id}`{tail}"

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
