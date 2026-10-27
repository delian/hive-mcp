"""'What is new for me': items added to the places a person can read, over the last few days.

Built per person from what they may read (the same filter as every other read), so a digest can
never show more than the person could find themselves. Their own items are left out.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING

from hive_mcp.domain.access import Tier
from hive_mcp.domain.errors import HiveError, ValidationError
from hive_mcp.domain.item import Cursor, ItemQuery, ItemView
from hive_mcp.ports.notify import Notification
from hive_mcp.services.models import provenance

if TYPE_CHECKING:
    from datetime import datetime

    from hive_mcp.domain.clock import Clock
    from hive_mcp.domain.principal import Principal
    from hive_mcp.ports.kv import KeyValueStore
    from hive_mcp.ports.notify import Notifier
    from hive_mcp.security.directory import UserDirectory
    from hive_mcp.services.knowledge import KnowledgeService
    from hive_mcp.services.promotions import PromotionService
    from hive_mcp.services.staleness import StaleReview

log = logging.getLogger(__name__)
MAX_DAYS = 90
MAX_SCANNED = 2000  # items examined per digest: bounds the cost on a very busy knowledge base
PAGE = 100
SENT_NS = "digest:sent"
CLAIM_TTL = 14 * 86400  # long enough to cover the week, short enough to be forgotten
QUIET_KINDS = frozenset({"session", "compaction"})  # reports are personal logs, not news


@dataclass(frozen=True, slots=True)
class DigestItem:
    id: str
    title: str
    kind: str
    author: str
    origin: str
    external: bool
    tags: tuple[str, ...]
    edited_by: str | None = None  # for the "edited" section: who made the latest change


@dataclass(frozen=True, slots=True)
class Digest:
    since: datetime
    until: datetime
    items: tuple[DigestItem, ...]
    total_new: int  # may exceed len(items) when the list was cut at `limit`
    top_tags: tuple[tuple[str, int], ...] = ()
    pending_approvals: int = 0
    scanned_all: bool = True
    by_origin: dict[str, int] = field(default_factory=dict)
    edited: tuple[DigestItem, ...] = ()  # older items someone else changed in the window
    total_edited: int = 0
    stale: tuple[DigestItem, ...] = ()  # the reader's own notes nobody has confirmed in a long time

    @property
    def empty(self) -> bool:
        return not self.items and not self.edited and not self.pending_approvals and not self.stale


class DigestService:
    def __init__(
        self,
        knowledge: KnowledgeService,
        promotions: PromotionService,
        clock: Clock,
        stale: StaleReview | None = None,
        stale_days: int = 0,
    ) -> None:
        self._knowledge, self._promotions, self._clock = knowledge, promotions, clock
        self._stale, self._stale_days = stale, stale_days

    def now(self) -> datetime:
        return self._clock.now()

    async def build(
        self,
        who: Principal,
        *,
        days: int = 7,
        limit: int = 30,
        include_external: bool = False,
    ) -> Digest:
        if not 1 <= days <= MAX_DAYS or not 1 <= limit <= 100:  # noqa: PLR2004
            raise ValidationError(f"days must be 1-{MAX_DAYS} and limit 1-100")
        until = self._clock.now()
        since = until - timedelta(days=days)
        found: list[DigestItem] = []
        tags: Counter[str] = Counter()
        origins: Counter[str] = Counter()
        scanned, cursor, complete, reached_older = 0, None, True, False
        while not reached_older:
            page = await self._knowledge.browse(
                who, ItemQuery(limit=PAGE, after=cursor, include_referenced=include_external)
            )
            for view in page.items:
                item = view.item
                if item.created_at < since:  # newest first: everything after this is older
                    reached_older = True
                    break
                scanned += 1
                if item.author_id == who.id or str(item.kind) in QUIET_KINDS:
                    continue
                label = provenance(item.scope, view.tier, item.author_id)
                origins[label] += 1
                tags.update(item.tags)
                found.append(self._entry(view, label))
            if not page.next_cursor:
                break
            if scanned >= MAX_SCANNED:
                complete = False
                break
            cursor = Cursor.decode(page.next_cursor)
        edited = await self._edited(who, since, include_external)
        pending = len(await self._promotions.list(who)) if who.is_approver else 0
        top = tuple(tags.most_common(8))
        return Digest(
            since, until, tuple(found[:limit]), len(found), top, pending, complete, dict(origins),
            tuple(edited[:limit]), len(edited), await self._needs_review(who),
        )  # fmt: skip

    async def _needs_review(self, who: Principal) -> tuple[DigestItem, ...]:
        if not (self._stale and self._stale_days):
            return ()
        page = await self._stale.stale(who, days=self._stale_days, limit=10)
        return tuple(
            self._entry(
                ItemView(s.item, Tier.PRIMARY),
                provenance(s.item.scope, Tier.PRIMARY, s.item.author_id),
            )
            for s in page.items
        )

    async def _edited(self, who: Principal, since: datetime, external: bool) -> list[DigestItem]:
        """Items from before the window that someone else changed during it, latest change first."""
        found: list[DigestItem] = []
        cursor, scanned = None, 0
        while scanned < MAX_SCANNED:
            query = ItemQuery(
                limit=PAGE, after=cursor, include_referenced=external, by_updated=True
            )
            page = await self._knowledge.browse(who, query)
            for view in page.items:
                item = view.item
                scanned += 1
                if item.updated_at < since:
                    return found
                if item.created_at >= since or item.revision < 2 or str(item.kind) in QUIET_KINDS:  # noqa: PLR2004
                    continue  # new items are already in the first list
                editor = await self._last_editor(who, item.id, item.author_id)
                if editor != who.id:
                    label = provenance(item.scope, view.tier, item.author_id)
                    found.append(replace(self._entry(view, label), edited_by=editor))
            if not page.next_cursor:
                break
            cursor = Cursor.decode(page.next_cursor)
        return found

    async def _last_editor(self, who: Principal, item_id: str, author: str) -> str:
        """Who produced the current version: the one who replaced the version before it."""
        try:
            versions = await self._knowledge.history(who, item_id, limit=2)
        except HiveError:
            return author
        previous = next((v for v in versions if v.replaced_by), None)
        return previous.replaced_by if previous and previous.replaced_by else author

    @staticmethod
    def _entry(view: ItemView, label: str) -> DigestItem:
        item = view.item
        external = view.tier is Tier.REFERENCED
        return DigestItem(
            item.id,
            item.title,
            str(item.kind),
            item.author_id,
            label,
            external,
            tuple(sorted(item.tags)),
        )


def render_text(digest: Digest, recipient: str = "") -> str:
    """Plain text for email and terminals. Titles and origins only: bodies are read in the tool."""
    lines = [f"What is new in the team knowledge base since {digest.since:%Y-%m-%d}"]
    if recipient:
        lines[0] += f" (for {recipient})"
    lines.append("")
    if digest.pending_approvals:
        waiting = f"{digest.pending_approvals} promotion request(s) wait for your decision"
        lines.append(f"{waiting} (ask your agent to run kb_list_promotions).\n")
    by_origin: dict[str, list[DigestItem]] = {}
    for item in digest.items:
        by_origin.setdefault(item.origin, []).append(item)
    for origin, items in by_origin.items():
        lines.append(f"{origin} ({digest.by_origin.get(origin, len(items))} new)")
        lines += [f"  - {i.title}  [{i.kind}, by {i.author}, id {i.id}]" for i in items]
        lines.append("")
    if digest.edited:
        lines.append(f"Changed by others ({digest.total_edited})")
        lines += [
            f"  - {i.title}  [{i.kind}, edited by {i.edited_by}, id {i.id}]" for i in digest.edited
        ]
        lines.append("")
    if digest.stale:
        lines.append(
            "Still true? These notes of yours have not been edited or confirmed for a while"
        )
        lines += [f"  - {i.title}  [id {i.id}]" for i in digest.stale]
        lines.append("  Ask your agent: kb_confirm <id> if it holds, or archive it with kb_update.")
        lines.append("")
    if digest.total_new > len(digest.items):
        lines.append(f"... and {digest.total_new - len(digest.items)} more (kb_digest lists them).")
    if digest.top_tags:
        lines.append("Busiest tags: " + ", ".join(f"{t} ({n})" for t, n in digest.top_tags))
    if not digest.scanned_all:
        lines.append("(Very busy week: only the newest items were examined.)")
    lines.append("\nOpen anything with kb_get <id> in your agent.")
    return "\n".join(lines).strip() + "\n"


class DigestSender:
    """Emails each person their own digest once per ISO week, when there is something to say."""

    def __init__(  # noqa: PLR0913, PLR0917 - collaborators
        self,
        digests: DigestService,
        directory: UserDirectory,
        email: Notifier,
        kv: KeyValueStore,
        clock: Clock,
        days: int = 7,
    ) -> None:
        self._digests, self._directory, self._email = digests, directory, email
        self._kv, self._clock, self._days = kv, clock, days

    async def send_all(self) -> tuple[int, int]:
        """(sent, failed). A person already sent this week is skipped, also across replicas."""
        year, week, _ = self._clock.now().isocalendar()
        sent = failed = 0
        for user in self._directory.list():
            if user.disabled:
                continue
            key = f"{year}-W{week:02d}:{user.email}"
            if not await self._kv.put_if_absent(
                SENT_NS, key, {"claimed": True}, ttl_seconds=14 * 86400
            ):
                continue
            try:
                digest = await self._digests.build(user.principal(), days=self._days)
                if not digest.empty:
                    subject = "Team knowledge digest"
                    note = Notification(
                        "digest", subject, render_text(digest, user.email), (user.email,)
                    )
                    await self._email.send(note)
                    sent += 1
            except HiveError:
                log.exception("digest.failed user=%s", user.email)
                await self._kv.delete(SENT_NS, key)  # release the claim: the next round retries
                failed += 1
        return sent, failed
