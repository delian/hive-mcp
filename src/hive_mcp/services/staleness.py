"""'Is this still true?': notes nobody has touched or confirmed for a long time.

Knowledge rots silently, and a stale note in shared memory misleads every agent that finds it.
An item is stale when neither an edit nor a confirmation is newer than the window. Confirming
(`kb_confirm`) records who vouched for it and when, without creating a new revision; to retire a
note, set its status (`kb_update`). Scans are bounded, newest-changed first.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from hive_mcp.domain import access
from hive_mcp.domain.errors import PermissionDenied, ValidationError
from hive_mcp.domain.item import Cursor, ItemQuery, Status
from hive_mcp.ports.audit import AuditEvent

if TYPE_CHECKING:
    from hive_mcp.domain.clock import Clock
    from hive_mcp.domain.item import Item
    from hive_mcp.domain.principal import Principal
    from hive_mcp.ports.audit import AuditLog
    from hive_mcp.ports.items import ItemRepository
    from hive_mcp.ports.kv import KeyValueStore

CONFIRMED_NS = "stale:confirmed"
DEFAULT_DAYS = 180
MIN_DAYS = 7
MAX_DAYS = 3650
PAGE = 100
MAX_SCANNED = 2000
AFTER_ANY_ID = "g"  # sorts after every hex item id
EXEMPT_KINDS = frozenset({"session", "compaction"})  # logs record what happened; they do not rot


@dataclass(frozen=True, slots=True)
class StaleItem:
    item: Item
    last_touched: datetime  # the later of the last edit and the last confirmation
    confirmed_by: str | None


@dataclass(frozen=True, slots=True)
class StalePage:
    items: list[StaleItem]
    next_cursor: str | None
    scanned_all: bool = True


class StaleReview:
    def __init__(
        self, items: ItemRepository, kv: KeyValueStore, audit: AuditLog, clock: Clock
    ) -> None:
        self._items, self._kv, self._audit, self._clock = items, kv, audit, clock

    async def _confirmation(self, item_id: str) -> tuple[datetime, str] | None:
        record = await self._kv.get(CONFIRMED_NS, item_id)
        if not record:
            return None
        return datetime.fromisoformat(record["at"]), str(record["by"])

    async def confirm(self, principal: Principal, item_id: str) -> datetime:
        view = await self._items.get(principal, item_id)
        item = view.item
        if not access.can_write(principal, item.acl, item.author_id):
            raise PermissionDenied("only people who can edit an item can confirm it")
        if item.status is not Status.ACTIVE:
            raise ValidationError(f"only active items need confirming (this one is {item.status})")
        now = self._clock.now()
        await self._kv.put(CONFIRMED_NS, item_id, {"at": now.isoformat(), "by": principal.id})
        await self._audit.append(
            AuditEvent(principal.org_id, principal.id, "item.confirm", now, item_id, {})
        )
        return now

    async def stale(
        self,
        principal: Principal,
        *,
        days: int = DEFAULT_DAYS,
        limit: int = 30,
        after: str | None = None,
        mine_only: bool = True,
    ) -> StalePage:
        """Stale active items you wrote (or, with mine_only=False, any you can edit)."""
        if not MIN_DAYS <= days <= MAX_DAYS or not 1 <= limit <= 100:  # noqa: PLR2004
            raise ValidationError(f"days must be {MIN_DAYS}-{MAX_DAYS} and limit 1-100")
        cutoff = self._clock.now() - timedelta(days=days)
        query = ItemQuery(
            statuses=frozenset({Status.ACTIVE}),
            include_referenced=False,
            limit=PAGE,
            by_updated=True,
            # start below the cutoff: fresh items are never listed, however many there are
            after=Cursor.decode(after) if after else Cursor(cutoff, AFTER_ANY_ID),
        )
        found: list[StaleItem] = []
        scanned = 0
        while scanned < MAX_SCANNED:
            page = await self._items.list(principal, query)
            for view in page.items:
                item = view.item
                scanned += 1
                if str(item.kind) in EXEMPT_KINDS:
                    continue
                if (
                    item.author_id != principal.id
                    if mine_only
                    else not access.can_write(principal, item.acl, item.author_id)
                ):
                    continue
                touched, by = item.updated_at, None
                if confirmation := await self._confirmation(item.id):
                    touched, by = max(confirmation[0], touched), confirmation[1]
                if touched >= cutoff:
                    continue
                found.append(StaleItem(item, touched, by))
                if len(found) >= limit:
                    cursor = Cursor(item.updated_at, item.id).encode()
                    return StalePage(found, cursor)
            if not page.next_cursor:
                return StalePage(found, None)
            query = replace(query, after=Cursor.decode(page.next_cursor))
        return StalePage(found, query.after.encode() if query.after else None, scanned_all=False)


def age_days(then: datetime, now: datetime) -> int:
    return max((now.astimezone(UTC) - then.astimezone(UTC)).days, 0)
