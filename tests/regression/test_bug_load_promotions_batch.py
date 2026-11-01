"""BUG-LOAD-3: an approver's list of pending requests read each request's note with its own query."""

from __future__ import annotations

from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.services.models import RememberRequest
from tests.conformance.factory import ALICE, ROOT

TEAM = Scope(ScopeLevel.TEAM, "t1")


async def test_the_approvers_queue_reads_all_pending_notes_in_one_batch(stack, monkeypatch):
    members = [Principal(f"m{n}", "org1", Role.MEMBER, teams=frozenset({"t1"})) for n in range(12)]
    for who in [ALICE, *members]:
        item = (
            await stack.knowledge.remember(who, RememberRequest(f"Note of {who.id}", "radar echo"))
        ).item
        await stack.promotions.request(who, item.id, TEAM, Visibility.SCOPE)
    items = stack.promotions._items
    calls = {"one": 0, "many": []}
    one, many = items.get_system, items.get_many_system

    async def count_one(*a, **k):
        calls["one"] += 1
        return await one(*a, **k)

    async def count_many(org_id, ids):
        calls["many"].append(len(list(ids)))
        return await many(org_id, ids)

    monkeypatch.setattr(items, "get_system", count_one)
    monkeypatch.setattr(items, "get_many_system", count_many)
    queue = await stack.promotions.list(ROOT)
    assert len(queue) == 13 and all(v.item is not None for v in queue)
    assert calls == {"one": 0, "many": [13]}  # one batched read for every pending request
    assert {v.item.title for v in queue} == {f"Note of {w.id}" for w in [ALICE, *members]}
    mine = await stack.promotions.list(ALICE)  # a requester sees no note text, and reads none
    assert [v.item for v in mine] == [None] and calls["many"] == [13]
