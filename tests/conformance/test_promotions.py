import asyncio
from dataclasses import replace

import pytest

from hive_mcp.domain.errors import Conflict, NotFound, ValidationError
from hive_mcp.domain.promotion import Promotion, PromotionStatus
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from tests.conformance.factory import ALICE, ORG1, P1, make_item

TEAM = Scope(ScopeLevel.TEAM, "t1")


def promo(backend, pid="p1", item_id="item1", org="org1", **kw):
    return Promotion(
        id=pid,
        org_id=org,
        item_id=item_id,
        requester_id="alice",
        scope=TEAM,
        visibility=Visibility.SCOPE,
        item_revision=1,
        created_at=backend.clock.now(),
        **kw,
    )


async def test_create_get_roundtrip_and_org_isolation(backend):
    made = await backend.promotions.create(promo(backend, reason="useful"))
    assert await backend.promotions.get("org1", "p1") == made
    assert await backend.promotions.get("org2", "p1") is None


async def test_one_pending_request_per_item_but_a_new_one_after_it_is_decided(backend):
    await backend.promotions.create(promo(backend, "p1"))
    with pytest.raises(Conflict):
        await backend.promotions.create(promo(backend, "p2"))
    with pytest.raises(Conflict):
        await backend.promotions.create(promo(backend, "p1", item_id="other"))  # id reuse
    await backend.promotions.transition(
        "org1", "p1", PromotionStatus.PENDING, PromotionStatus.REJECTED,
        by="root", note="no", at=backend.clock.now(),
    )  # fmt: skip
    await backend.promotions.create(promo(backend, "p2"))


async def test_transition_is_compare_and_set(backend):
    await backend.promotions.create(promo(backend))
    now = backend.clock.now()
    ok = await backend.promotions.transition(
        "org1",
        "p1",
        PromotionStatus.PENDING,
        PromotionStatus.APPROVED,
        by="root",
        note="ok",
        at=now,
    )
    assert ok and ok.status is PromotionStatus.APPROVED and ok.decided_by == "root"
    assert ok.decided_at == now and ok.note == "ok"
    again = await backend.promotions.transition(
        "org1", "p1", PromotionStatus.PENDING, PromotionStatus.REJECTED, by="x", note="", at=now
    )
    assert again is None  # lost the race
    assert (await backend.promotions.get("org1", "p1")).status is PromotionStatus.APPROVED
    assert (
        await backend.promotions.transition(
            "org2", "p1", PromotionStatus.APPROVED, PromotionStatus.STALE, by="z", note="", at=now
        )
        is None
    )  # other tenant cannot touch it


async def test_illegal_transitions_are_refused(backend):
    await backend.promotions.create(promo(backend))
    for expected, to in [
        (PromotionStatus.PENDING, PromotionStatus.PENDING),
        (PromotionStatus.APPROVED, PromotionStatus.REJECTED),
        (PromotionStatus.REJECTED, PromotionStatus.APPROVED),
    ]:
        with pytest.raises(ValidationError):
            await backend.promotions.transition(
                "org1", "p1", expected, to, by="r", note="", at=backend.clock.now()
            )


async def test_concurrent_deciders_exactly_one_wins(backend):
    await backend.promotions.create(promo(backend))

    async def decide(i):
        return await backend.promotions.transition(
            "org1", "p1", PromotionStatus.PENDING, PromotionStatus.APPROVED,
            by=f"admin{i}", note="", at=backend.clock.now(),
        )  # fmt: skip

    results = await asyncio.gather(*(decide(i) for i in range(8)))
    assert sum(r is not None for r in results) == 1


async def test_list_filters_orders_and_scopes_by_org(backend):
    for i, who in enumerate(["alice", "bob", "alice"]):
        backend.clock.advance(seconds=1)
        await backend.promotions.create(
            replace(promo(backend, f"p{i}", item_id=f"i{i}"), requester_id=who)
        )
    await backend.promotions.create(promo(backend, "z", item_id="iz", org="org2"))
    await backend.promotions.transition(
        "org1", "p0", PromotionStatus.PENDING, PromotionStatus.WITHDRAWN,
        by="alice", note="", at=backend.clock.now(),
    )  # fmt: skip
    ids = lambda rows: [r.id for r in rows]  # noqa: E731
    assert ids(await backend.promotions.list("org1")) == ["p0", "p1", "p2"]
    assert ids(await backend.promotions.list("org1", status=PromotionStatus.PENDING)) == [
        "p1",
        "p2",
    ]
    assert ids(await backend.promotions.list("org1", requester_id="alice")) == ["p0", "p2"]
    assert ids(await backend.promotions.list("org1", limit=1)) == ["p0"]
    assert ids(await backend.promotions.list("org2")) == ["z"]


async def test_relocate_system_moves_checks_revision_and_tenant(backend):
    item = make_item(backend.clock, scope=P1)
    await backend.items.create(ALICE, item)
    moved = await backend.items.relocate_system("org1", item.id, ORG1, Visibility.SCOPE, 1)
    assert moved.scope == ORG1 and moved.revision == 2
    stored = await backend.items.get_system("org1", item.id)
    assert stored == moved
    with pytest.raises(Conflict):
        await backend.items.relocate_system("org1", item.id, P1, Visibility.SCOPE, 1)  # stale
    with pytest.raises(NotFound):
        await backend.items.relocate_system("org2", item.id, P1, Visibility.SCOPE, 2)
    with pytest.raises(ValidationError):
        await backend.items.relocate_system(
            "org1", item.id, Scope(ScopeLevel.PRIVATE, "alice"), Visibility.ORG, 2
        )
