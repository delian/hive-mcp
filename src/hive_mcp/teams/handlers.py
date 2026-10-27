"""What the Teams bot does with a message. Replies are plain text (no markdown to inject into).

In a personal chat the person gets full answers and can save private notes. In a channel or group
chat the audience is everyone there, so (as with Slack mentions) only org-wide knowledge and the
channel's mapped project are quoted, a save names nothing it saved, and the bot answers only when
an admin has allowed channel use.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from hive_mcp.domain.errors import HiveError
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.mcp_api.errors import user_message
from hive_mcp.security.directory import normalize_email
from hive_mcp.services.models import ConsultRequest, RememberRequest
from hive_mcp.slack.handlers import audience_filter

if TYPE_CHECKING:
    from collections.abc import Callable

    from hive_mcp.bootstrap import Services
    from hive_mcp.domain.principal import Principal
    from hive_mcp.integrations.config import IntegrationConfig
    from hive_mcp.ports.kv import KeyValueStore
    from hive_mcp.security.directory import UserDirectory
    from hive_mcp.teams.connector import BotConnector

log = logging.getLogger("hive.teams")
AT_TAG = re.compile(
    r"<at[^<>]{0,200}>[^<]{0,200}</at>", re.IGNORECASE
)  # bounded: no backtracking blowup
TAGS = re.compile(r"<[^>]{1,200}>")
RESULTS = 5
MAX_NOTE = 20_000
MAX_TITLE = 80
EMAIL_NS = "teams:email"
REF_NS = "teams:ref"  # where to reach a person for notifications (their personal chat)
CACHE_SECONDS = 3600
MISS_SECONDS = 60  # a failed or empty lookup is retried soon: the connector may only have hiccuped
HELP = (
    "Hive: the team's shared memory.\n\n"
    "Ask a question to search what the team knows.\n"
    "save <text> keeps a note (private to you in a personal chat).\n"
    "In channels I only quote organisation-wide knowledge and the channel's project."
)
NOT_LINKED = (
    "I could not match your Teams account to a Hive user. Ask an admin to add your work email "
    "(hive user add), then try again."
)


def obj(value: object) -> dict[str, Any]:
    """`value` if it is a JSON object, else an empty one: activity fields may have any shape."""
    return value if isinstance(value, dict) else {}


def base_conversation(conversation: str) -> str:
    """A reply inside a channel thread has the id `<channel>;messageid=<post>`: the channel is
    what an admin maps to a project."""
    return conversation.partition(";")[0]


def clean_text(raw: str) -> str:
    """The message without @mention tags and other markup, unescaped."""
    return html.unescape(TAGS.sub("", AT_TAG.sub("", raw))).strip()


class TeamsBot:
    def __init__(  # noqa: PLR0913, PLR0917 - collaborators
        self,
        config: IntegrationConfig,
        connector: BotConnector,
        directory: UserDirectory,
        get_services: Callable[[], Services],
        get_kv: Callable[[], KeyValueStore],
        clock: Callable[[], float] = time.monotonic,
        *,
        note_base: str | None = None,
    ) -> None:
        self.note_base = note_base  # where notes can be opened (None: no links to offer)
        self._cfg, self._connector, self._directory = config, connector, directory
        self._services, self._get_kv, self._clock = get_services, get_kv, clock

    def services(self) -> Services:
        return self._services()

    def kv(self) -> KeyValueStore:
        return self._get_kv()

    async def invoke(self, activity: dict[str, Any]) -> dict[str, Any]:
        """Message extension commands (search, save a message); see extension.py."""
        from hive_mcp.teams.extension import TeamsExtension  # noqa: PLC0415 - imports this module

        return await TeamsExtension(self, self._cfg.workspace, self._cfg.channel_projects).invoke(
            activity
        )

    async def handle(self, activity: dict[str, Any]) -> None:
        if activity.get("type") != "message" or not activity.get("text"):
            return  # conversation updates, typing, reactions...: nothing to do
        if obj(obj(activity.get("channelData")).get("tenant")).get("id") != self._cfg.workspace:
            return  # another organisation's Teams
        conversation = obj(activity.get("conversation"))
        personal = conversation.get("conversationType") == "personal"
        if not personal and not self._cfg.allow_mentions:
            return
        who = await self.resolve(activity)
        if who is not None and personal:  # remember where to reach them for notifications
            reference = {
                "serviceUrl": str(activity.get("serviceUrl", "")),
                "conversation": str(conversation.get("id", "")),
            }
            await self._get_kv().put(REF_NS, who.id, reference)
        text = clean_text(str(activity["text"]))
        answer = await self._answer(who, text, str(conversation.get("id", "")), personal)
        await self._send(activity, answer)

    async def _answer(self, who: Principal | None, text: str, conv: str, personal: bool) -> str:
        if who is None:
            return NOT_LINKED
        if text.lower() in ("", "help"):
            return HELP
        try:
            if text.split(None, 1)[0].lower() == "save":
                return await self._save(who, text.partition(" ")[2], conv, personal)
            return await self._search(who, text, conv, personal)
        except HiveError as exc:
            return f"Sorry: {user_message(exc)}"

    def _project(self, who: Principal, conv: str) -> str | None:
        project = self._cfg.channel_projects.get(base_conversation(conv))
        return project if project and project in who.projects else None

    async def _search(self, who: Principal, text: str, conv: str, personal: bool) -> str:
        result = await self._services().consult.consult(who, ConsultRequest(text, limit=10))
        matches = list(result.primary) + (list(result.referenced) if personal else [])
        if not personal:
            matches = audience_filter(matches, self._project(who, conv))
        matches = matches[:RESULTS]
        if not matches:
            return f"Nothing found for: {text}" + (
                ""
                if personal
                else "\n\nI only quote shared knowledge in channels; message me directly for more."
            )
        lines = [f"{len(matches)} result(s) for: {text}"]
        lines += [
            f"{m.title} ({m.provenance})\n{m.snippet.replace(chr(10), ' ')[:200]}" for m in matches
        ]
        return "\n\n".join(lines)

    async def _save(self, who: Principal, body: str, conv: str, personal: bool) -> str:
        body = body.strip()[:MAX_NOTE]
        if not body:
            return "Nothing to save: save <text>"
        project = self._project(who, conv)
        saved = await self._services().knowledge.remember(
            who,
            RememberRequest(
                title=body.splitlines()[0][:MAX_TITLE],
                body=body,
                tags=("source/teams",),
                scope=Scope(ScopeLevel.PROJECT, project) if project else None,
                source=f"teams:{conv}"[:400],
                idempotency_key=f"teams:{conv}:{hashlib.sha256(body.encode()).hexdigest()[:24]}:{who.id}"[
                    :200
                ],
            ),
        )
        verb = "Saved" if saved.created else "Already saved"
        if not personal:
            return f"{verb}." + (
                f" Shared with project {project}." if project else " (private to you)"
            )
        where = f"to project {project}" if project else "as a private note"
        return f"{verb} {where}: {saved.item.title}"

    async def resolve(self, activity: dict[str, Any]) -> Principal | None:
        aad = str(obj(activity.get("from")).get("aadObjectId", ""))
        if not aad:
            return None
        email = self._cfg.users.get(aad) or await self._email(activity, aad)
        if not email:
            return None
        try:
            user = self._directory.lookup(normalize_email(email))
        except Exception:
            return None
        return user.principal() if user else None

    async def _email(self, activity: dict[str, Any], aad: str) -> str | None:
        key = f"{self._cfg.workspace}:{aad}"
        cached = await self._get_kv().get(EMAIL_NS, key)
        if cached:
            return str(cached["email"]) or None
        conversation = str(obj(activity.get("conversation")).get("id", ""))
        email = await self._connector.member_email(
            str(activity.get("serviceUrl", "")), conversation,
            str(obj(activity.get("from")).get("id", "")), self._clock(),
        )  # fmt: skip
        await self._get_kv().put(
            EMAIL_NS,
            key,
            {"email": email or ""},
            ttl_seconds=CACHE_SECONDS if email else MISS_SECONDS,
        )
        return email

    async def _send(self, activity: dict[str, Any], text: str) -> None:
        conversation = str(obj(activity.get("conversation")).get("id", ""))
        try:
            await self._connector.reply(
                str(activity.get("serviceUrl", "")), conversation, str(activity.get("id", "")),
                text, self._clock(),
            )  # fmt: skip
        except HiveError:
            log.warning("teams reply failed")
