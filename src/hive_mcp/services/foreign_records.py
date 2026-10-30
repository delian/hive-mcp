"""Per-person records behind the foreign-note tools: hidden notes and edit suggestions."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hive_mcp.domain.principal import Principal
    from hive_mcp.ports.kv import KeyValueStore

NS_IGNORED, NS_SUGGEST = "foreign:ignored", "foreign:suggest"
MAX_IGNORED = 1000  # per person
DECIDED_TTL = 30 * 86400.0


class HiddenNotes:
    """The other-group notes each person chose to hide from their own search results."""

    def __init__(self, kv: KeyValueStore) -> None:
        self._kv = kv

    @staticmethod
    def prefix(who: Principal) -> str:
        return f"{who.org_id}:{who.id}:"

    async def ids(self, who: Principal) -> frozenset[str]:
        prefix = self.prefix(who)
        held = await self._kv.scan(NS_IGNORED, prefix, MAX_IGNORED)
        return frozenset(key[len(prefix) :] for key, _ in held)


MAX_ERASE_SCAN = 5000


async def erase_person(kv: KeyValueStore, org_id: str, user_id: str, alias: str | None) -> int:
    """Forgetting a person: drop their hidden-note list and open suggestions; with an alias also
    rename them on the decided suggestions that remain. Returns how many records changed."""
    changed = 0
    for key, _ in await kv.scan(NS_IGNORED, f"{org_id}:{user_id}:", MAX_ERASE_SCAN):
        await kv.delete(NS_IGNORED, key)
        changed += 1
    for key, rec in await kv.scan(NS_SUGGEST, f"{org_id}:", MAX_ERASE_SCAN):
        mine = rec["proposer"] == user_id
        if mine and rec["status"] == "open":
            await kv.delete(NS_SUGGEST, key)
            changed += 1
        elif alias and (mine or rec.get("decided_by") == user_id):
            renamed = {**rec, "proposer": alias if mine else rec["proposer"]}
            if rec.get("decided_by") == user_id:
                renamed["decided_by"] = alias
            await kv.put(NS_SUGGEST, key, renamed, ttl_seconds=DECIDED_TTL)
            changed += 1
    return changed
