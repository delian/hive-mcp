"""Promotion requests and decisions raced against each other: one decision wins, nothing is applied
twice, and an approval never publishes content nobody reviewed."""

from __future__ import annotations

import pytest

from hive_mcp.domain.errors import Conflict, HiveError
from hive_mcp.domain.item import ItemPatch
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.promotion import PromotionStatus
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.services.models import RememberRequest
from hive_mcp.services.promotions import APPLY_JOB
from hive_mcp.services.runner import JobRunner
from tests.stress.support import SCALE, gather_bounded

pytestmark = pytest.mark.stress

ORG = Scope(ScopeLevel.ORG, "acme")
ANN = Principal("ann", "acme", Role.MEMBER, projects=frozenset({"p1"}))
ADMINS = [Principal(f"adm{i}", "acme", Role.ADMIN) for i in range(5)]
MAIN_ADMIN = ADMINS[0]


async def private_note(stack, title: str, who: Principal = ANN):
    return (await stack.knowledge.remember(who, RememberRequest(title, f"body of {title}"))).item


async def drain_jobs(stack) -> int:
    """Run every queued apply job (the clock is moved past the 60 s delay first)."""
    stack.backend.clock.advance(seconds=120)
    runner = JobRunner(
        stack.backend.jobs,
        {APPLY_JOB: stack.promotions.handle_apply},
        worker_id="w",
        lease_seconds=30,
    )
    n = 0
    while await runner.run_once():
        n += 1
    return n


async def approved_count(stack, item_id: str) -> int:
    found = await stack.backend.audit.list(
        "acme", action="promotion.approved", target_id=item_id, limit=100
    )
    return len(found)


async def test_concurrent_requests_for_one_item_create_exactly_one_pending(stack) -> None:
    note = await private_note(stack, "Request race")
    racers = 12 * SCALE
    results = await gather_bounded(
        [(lambda: stack.promotions.request(ANN, note.id, ORG, Visibility.ORG, "please"))] * racers
    )
    wins = [r for r in results if not isinstance(r, BaseException)]
    losses = [r for r in results if isinstance(r, BaseException)]
    assert len(wins) == 1
    assert all(isinstance(e, Conflict) for e in losses), losses
    pending = await stack.promotions.list(MAIN_ADMIN, PromotionStatus.PENDING)
    assert [p.promotion.id for p in pending] == [wins[0].id]


async def test_many_approvers_one_decision_the_item_moves_exactly_once(stack) -> None:
    count = 8 * SCALE
    notes = [await private_note(stack, f"Approve race {i}") for i in range(count)]
    promos = [
        await stack.promotions.request(ANN, n.id, ORG, Visibility.ORG, "please") for n in notes
    ]
    jobs = [
        (lambda p=p, a=a: stack.promotions.decide(a, p.id, "approve"))
        for p in promos
        for a in ADMINS
        for _ in range(2)  # the same admin may even click twice
    ]
    results = await gather_bounded(jobs, 64)
    per_item = len(ADMINS) * 2
    for i, promo in enumerate(promos):
        group = results[i * per_item : (i + 1) * per_item]
        wins = [r for r in group if not isinstance(r, BaseException)]
        assert len(wins) == 1 and wins[0].status is PromotionStatus.APPROVED
        assert all(isinstance(e, Conflict) for e in group if isinstance(e, BaseException))
        stored = await stack.backend.promotions.get("acme", promo.id)
        assert stored.status is PromotionStatus.APPROVED and stored.decided_by == wins[0].decided_by
    # the repair jobs run after the inline move: they must find nothing left to do
    await drain_jobs(stack)
    for note in notes:
        item = (await stack.knowledge.get(MAIN_ADMIN, note.id)).item
        assert (item.scope, item.visibility) == (ORG, Visibility.ORG)
        assert item.revision == note.revision + 1, "relocated more than once"
        assert await approved_count(stack, note.id) == 1
    assert await drain_jobs(stack) == 0  # nothing left behind, nothing duplicated


async def test_approve_reject_and_withdraw_race_yields_one_final_state(stack) -> None:
    count = 8 * SCALE
    notes = [await private_note(stack, f"Mixed decision {i}") for i in range(count)]
    promos = [
        await stack.promotions.request(ANN, n.id, ORG, Visibility.ORG, "please") for n in notes
    ]
    plan = [
        ("approve", ADMINS[1]), ("approve", ADMINS[2]), ("reject", ADMINS[3]),
        ("reject", ADMINS[4]), ("withdraw", ANN), ("withdraw", ANN),
    ]  # fmt: skip
    jobs = [
        (lambda p=p, d=d, who=who: stack.promotions.decide(who, p.id, d))
        for p in promos
        for d, who in plan
    ]
    results = await gather_bounded(jobs, 64)
    for i, (promo, note) in enumerate(zip(promos, notes, strict=True)):
        group = results[i * len(plan) : (i + 1) * len(plan)]
        wins = [r for r in group if not isinstance(r, BaseException)]
        assert len(wins) == 1, [type(r).__name__ for r in group]
        assert all(isinstance(e, HiveError) for e in group if isinstance(e, BaseException))
        stored = await stack.backend.promotions.get("acme", promo.id)
        assert stored.status is wins[0].status
        item = (await stack.knowledge.get(ANN, note.id)).item
        moved = item.scope == ORG
        assert moved == (stored.status is PromotionStatus.APPROVED)
        assert item.revision == note.revision + (1 if moved else 0)
    await drain_jobs(stack)
    for promo, note in zip(promos, notes, strict=True):  # a late job changes nothing
        stored = await stack.backend.promotions.get("acme", promo.id)
        item = (await stack.knowledge.get(ANN, note.id)).item
        assert (item.scope == ORG) == (stored.status is PromotionStatus.APPROVED)


async def test_an_edit_racing_an_approval_never_publishes_unreviewed_text(stack) -> None:
    count = 10 * SCALE
    notes = [await private_note(stack, f"Edit vs approve {i}") for i in range(count)]
    promos = [
        await stack.promotions.request(ANN, n.id, ORG, Visibility.ORG, "please") for n in notes
    ]

    async def edit(note) -> object:
        return await stack.knowledge.update(
            ANN, note.id, ItemPatch(body=f"CHANGED after review {note.id}"), note.revision
        )

    jobs = []
    for p, n in zip(promos, notes, strict=True):
        pair = [
            (lambda p=p: stack.promotions.decide(MAIN_ADMIN, p.id, "approve")),
            (lambda n=n: edit(n)),
        ]
        jobs += pair  # the order of the pair alternates in the result bookkeeping below
    order = list(range(len(jobs)))
    for k in range(1, count, 2):  # odd items: the edit is scheduled before the approval
        order[2 * k], order[2 * k + 1] = order[2 * k + 1], order[2 * k]
    shuffled = await gather_bounded([jobs[o] for o in order], 64)
    results: list = [None] * len(jobs)
    for pos, o in enumerate(order):
        results[o] = shuffled[pos]
    await drain_jobs(stack)
    published_reviewed = published_blocked = 0
    for i, (promo, note) in enumerate(zip(promos, notes, strict=True)):
        approve, edited = results[2 * i], results[2 * i + 1]
        stored = await stack.backend.promotions.get("acme", promo.id)
        item = (await stack.knowledge.get(ANN, note.id)).item
        if stored.status is PromotionStatus.APPROVED:
            # the move happened to exactly the reviewed revision; a later edit stacked on top
            assert item.scope == ORG and not isinstance(approve, BaseException)
            history = await stack.knowledge.history(ANN, note.id, 10)
            moved_at = next(v for v in reversed(history) if v.scope == ORG)
            assert moved_at.body == note.body, "unreviewed text became org-visible by approval"
            published_reviewed += 1
        else:
            assert stored.status is PromotionStatus.STALE, stored.status
            assert item.scope != ORG and isinstance(approve, BaseException)
            assert not isinstance(edited, BaseException)
            published_blocked += 1
    assert published_reviewed + published_blocked == count
