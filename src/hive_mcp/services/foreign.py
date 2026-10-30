"""What a person can do with another group's note: adopt it (fork), hide it, or propose a change.

* fork copies the note into the caller's own scope, linked `derived_from`; diff_upstream shows
  how the original has moved on since.
* ignore hides one other-group note from the caller's own search results (not for anyone else).
* suggest_edit leaves a proposal for the note's owner (anyone who may edit it), who accepts or
  rejects it; accepting applies exactly the reviewed revision, like a promotion does.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from hive_mcp.domain import access
from hive_mcp.domain.access import Tier
from hive_mcp.domain.errors import NotFound, PermissionDenied, ValidationError
from hive_mcp.domain.item import ItemPatch
from hive_mcp.domain.scope import Scope, ScopeLevel, scope_key
from hive_mcp.domain.text import check_text
from hive_mcp.ports.audit import AuditEvent
from hive_mcp.security.injection import FLAG_TAG
from hive_mcp.security.sanitize import sanitize
from hive_mcp.services.foreign_records import (
    DECIDED_TTL,
    MAX_IGNORED,
    NS_IGNORED,
    NS_SUGGEST,
    HiddenNotes,
)
from hive_mcp.services.models import RememberRequest, RememberResult

if TYPE_CHECKING:
    from hive_mcp.domain.clock import Clock
    from hive_mcp.domain.principal import Principal
    from hive_mcp.ports.audit import AuditLog
    from hive_mcp.ports.kv import JsonObject, KeyValueStore
    from hive_mcp.services.graph import GraphService
    from hive_mcp.services.knowledge import KnowledgeService

MAX_OPEN_PER_PROPOSER = 20
MAX_LISTED = 50
MAX_SCANNED = 500  # suggestions read per listing
MAX_NOTE = 1000
MAX_DIFF_LINES = 200
OPEN_TTL = 180 * 86400.0
FORK_SOURCE = re.compile(r"fork:([0-9a-f]{32})@(\d+)")


@dataclass(frozen=True, slots=True)
class UpstreamDiff:
    upstream_id: str
    forked_revision: int
    upstream_revision: int | None  # None: the original is gone or no longer visible
    identical: bool
    diff: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class Suggestion:
    id: str
    item_id: str
    item_title: str
    item_revision: int
    proposer: str
    title: str | None
    body: str | None
    note: str
    status: str
    created_at: str


def _suggestion(rec: JsonObject) -> Suggestion:
    return Suggestion(
        id=rec["id"], item_id=rec["item_id"], item_title=rec["item_title"],
        item_revision=rec["item_revision"], proposer=rec["proposer"], title=rec["title"],
        body=rec["body"], note=rec["note"], status=rec["status"], created_at=rec["created_at"],
    )  # fmt: skip


class ForeignService:
    def __init__(
        self,
        knowledge: KnowledgeService,
        graph: GraphService,
        kv: KeyValueStore,
        audit: AuditLog,
        clock: Clock,
    ) -> None:
        self._knowledge, self._graph, self._kv = knowledge, graph, kv
        self._audit, self._clock = audit, clock

    async def _log(self, who: Principal, action: str, target: str, **details: str) -> None:
        event = AuditEvent(who.org_id, who.id, action, self._clock.now(), target, details)
        await self._audit.append(event)

    # ------------------------------------------------------------------ fork
    async def fork(
        self, who: Principal, item_id: str, scope: Scope | None = None
    ) -> RememberResult:
        item = (await self._knowledge.get(who, item_id)).item
        target = scope or Scope(ScopeLevel.PRIVATE, who.id)
        result = await self._knowledge.remember(
            who,
            RememberRequest(
                title=item.title, body=item.body, kind=item.kind, scope=target,
                tags=tuple(sorted(item.tags - {FLAG_TAG})),
                idempotency_key=f"fork:{item.id}@{item.revision}:{scope_key(target)}",
                source=f"fork:{item.id}@{item.revision}",
            ),
        )  # fmt: skip
        await self._graph.link(who, result.item.id, item.id, "derived_from")
        await self._log(who, "item.fork", result.item.id, upstream=item.id, scope=scope_key(target))
        return result

    async def diff_upstream(self, who: Principal, item_id: str) -> UpstreamDiff:
        mine = (await self._knowledge.get(who, item_id)).item
        match = FORK_SOURCE.fullmatch(mine.source or "")
        if not match:
            raise ValidationError("this note was not made with kb_fork")
        upstream_id, forked = match.group(1), int(match.group(2))
        try:
            upstream = (await self._knowledge.get(who, upstream_id)).item
        except NotFound:
            return UpstreamDiff(upstream_id, forked, None, False, "", False)
        lines = list(
            difflib.unified_diff(
                f"{mine.title}\n{mine.body}".splitlines(),
                f"{upstream.title}\n{upstream.body}".splitlines(),
                "your fork", "upstream", lineterm="", n=2,
            )
        )  # fmt: skip
        shown = lines[:MAX_DIFF_LINES]
        return UpstreamDiff(
            upstream_id, forked, upstream.revision, not lines, "\n".join(shown),
            len(lines) > len(shown),
        )  # fmt: skip

    # ------------------------------------------------------------------ ignore
    async def ignore(self, who: Principal, item_id: str, ignored: bool = True) -> None:
        key = HiddenNotes.prefix(who) + item_id
        check_text(item_id, what="id")
        if not ignored:
            await self._kv.delete(NS_IGNORED, key)
            await self._log(who, "item.unignore", item_id)
            return
        view = await self._knowledge.get(who, item_id)
        if view.tier is not Tier.REFERENCED:
            raise ValidationError(
                "only notes from other groups can be hidden; archive your own with kb_update"
            )
        held = await self._kv.scan(NS_IGNORED, HiddenNotes.prefix(who), MAX_IGNORED + 1)
        if len(held) >= MAX_IGNORED and all(k != key for k, _ in held):
            raise ValidationError(f"at most {MAX_IGNORED} notes can be hidden; unhide some first")
        await self._kv.put(NS_IGNORED, key, {"at": self._clock.now().isoformat()})
        await self._log(who, "item.ignore", item_id)

    async def ignored_ids(self, who: Principal) -> frozenset[str]:
        return await HiddenNotes(self._kv).ids(who)

    # ------------------------------------------------------------------ suggestions
    async def suggest_edit(
        self, who: Principal, item_id: str, *, title: str | None, body: str | None, note: str
    ) -> Suggestion:
        view = await self._knowledge.get(who, item_id)
        item = view.item
        if access.can_write(who, item.acl, item.author_id):
            raise ValidationError("you can change this note yourself with kb_update")
        if title is None and body is None:
            raise ValidationError("give the new title and/or the new body")
        if len(note) > MAX_NOTE:
            raise ValidationError(f"the note must be at most {MAX_NOTE} characters")
        check_text(note, what="note")
        clean = sanitize(title if title is not None else item.title, body or item.body)
        ItemPatch(title=clean.title, body=clean.body)  # the same size and text rules as an edit
        if (clean.title, clean.body) == (item.title, item.body):
            raise ValidationError("the suggestion changes nothing")
        mine = [
            r for _, r in await self._kv.scan(NS_SUGGEST, f"{who.org_id}:", MAX_SCANNED)
            if r["proposer"] == who.id and r["status"] == "open"
        ]  # fmt: skip
        sid = hashlib.sha256(f"{item.id}:{who.id}".encode()).hexdigest()[:16]
        if len(mine) >= MAX_OPEN_PER_PROPOSER and all(r["id"] != sid for r in mine):
            raise ValidationError(f"you already have {MAX_OPEN_PER_PROPOSER} open suggestions")
        rec: JsonObject = {
            "id": sid, "item_id": item.id, "item_title": item.title,
            "item_revision": item.revision, "proposer": who.id,
            "title": clean.title if title is not None else None,
            "body": clean.body if body is not None else None,
            "note": note, "status": "open", "created_at": self._clock.now().isoformat(),
        }  # fmt: skip
        await self._kv.put(NS_SUGGEST, f"{who.org_id}:{sid}", rec, ttl_seconds=OPEN_TTL)
        await self._log(who, "suggestion.create", item.id, suggestion=sid)
        return _suggestion(rec)

    async def _can_review(self, who: Principal, rec: JsonObject) -> bool:
        try:
            item = (await self._knowledge.get(who, rec["item_id"])).item
        except NotFound:
            return False
        return access.can_write(who, item.acl, item.author_id)

    async def list_suggestions(
        self, who: Principal, include_decided: bool = False
    ) -> list[Suggestion]:
        """The open suggestions for notes the caller may edit, newest first."""
        found = await self._kv.scan(NS_SUGGEST, f"{who.org_id}:", MAX_SCANNED)
        keep = [r for _, r in found if include_decided or r["status"] == "open"]
        keep.sort(key=lambda r: r["created_at"], reverse=True)
        out: list[Suggestion] = []
        for rec in keep:
            if len(out) >= MAX_LISTED:
                break
            if await self._can_review(who, rec):
                out.append(_suggestion(rec))
        return out

    async def decide_suggestion(
        self, who: Principal, suggestion_id: str, decision: Literal["accept", "reject"]
    ) -> Suggestion:
        check_text(suggestion_id, what="id")
        key = f"{who.org_id}:{suggestion_id}"
        rec = await self._kv.get(NS_SUGGEST, key)
        if rec is None or not await self._can_review(who, rec):
            raise NotFound("no such suggestion")  # also when the caller may not review it
        if rec["status"] != "open":
            raise ValidationError(f"this suggestion was already {rec['status']}")
        item = (await self._knowledge.get(who, rec["item_id"])).item
        if not access.can_write(who, item.acl, item.author_id):
            raise PermissionDenied(item.id)
        if decision == "accept":
            if item.revision != rec["item_revision"]:
                raise ValidationError(
                    f"the note changed since this was suggested (revision {rec['item_revision']}, "
                    f"now {item.revision}): reject it and ask for a new suggestion"
                )
            patch = ItemPatch(title=rec["title"], body=rec["body"])
            await self._knowledge.update(who, item.id, patch, item.revision)
        status = "accepted" if decision == "accept" else "rejected"
        rec = {**rec, "status": status, "decided_by": who.id,
               "decided_at": self._clock.now().isoformat()}  # fmt: skip
        await self._kv.put(NS_SUGGEST, key, rec, ttl_seconds=DECIDED_TTL)
        await self._log(who, f"suggestion.{decision}", item.id, suggestion=suggestion_id)
        return _suggestion(rec)
