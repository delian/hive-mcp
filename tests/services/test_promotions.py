import asyncio

import pytest

from hive_mcp.domain.errors import (
    Conflict,
    NotFound,
    PermissionDenied,
    StorageBusy,
    ValidationError,
)
from hive_mcp.domain.item import ItemPatch
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.promotion import PromotionStatus
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.services.models import ConsultRequest, RememberRequest
from hive_mcp.services.promotions import APPLY_JOB
from tests.conformance.factory import ALICE, BOB, CAROL, ROOT

TEAM = Scope(ScopeLevel.TEAM, "t1")
ORG = Scope(ScopeLevel.ORG, "org1")
ROOT2 = Principal("root2", "org1", Role.ADMIN)
OUTSIDE_ADMIN = Principal("zoe", "org2", Role.ADMIN)


async def note(stack, who=ALICE, **kw):
    kw = {"title": "Pulse timing", "body": "radar echo delay gives range", **kw}
    return (await stack.knowledge.remember(who, RememberRequest(**kw))).item


async def test_member_cannot_publish_to_team_directly_but_can_request(stack):
    item = await note(stack)
    with pytest.raises(PermissionDenied, match="kb_request_promotion"):
        await stack.knowledge.move(ALICE, item.id, TEAM, Visibility.SCOPE, item.revision)
    promo = await stack.promotions.request(ALICE, item.id, TEAM, Visibility.SCOPE, " handy ")
    assert promo.status is PromotionStatus.PENDING and promo.reason == "handy"
    assert promo.item_revision == item.revision


async def test_approval_moves_the_item_and_makes_it_findable_by_the_team(stack):
    item = await note(stack)
    teammate = Principal("tim", "org1", Role.MEMBER, teams=frozenset({"t1"}))
    assert not (await stack.consult.consult(teammate, ConsultRequest("radar echo"))).primary
    promo = await stack.promotions.request(ALICE, item.id, TEAM, Visibility.SCOPE)
    done = await stack.promotions.decide(ROOT, promo.id, "approve", "good")
    assert done.status is PromotionStatus.APPROVED and done.decided_by == "root"
    moved = await stack.backend.items.get_system("org1", item.id)
    assert moved.scope == TEAM and moved.revision == item.revision + 1
    found = await stack.consult.consult(teammate, ConsultRequest("radar echo"))
    assert [m.item_id for m in found.primary] == [item.id]  # indexes follow the new ACL
    actions = [r.event.action for r in await stack.backend.audit.list("org1")]
    assert "promotion.request" in actions and "promotion.approved" in actions


async def test_reject_and_withdraw_leave_the_item_where_it_was(stack):
    item = await note(stack)
    promo = await stack.promotions.request(ALICE, item.id, TEAM, Visibility.SCOPE)
    rejected = await stack.promotions.decide(ROOT, promo.id, "reject", "too vague")
    assert rejected.status is PromotionStatus.REJECTED and rejected.note == "too vague"
    assert (await stack.backend.items.get_system("org1", item.id)).scope.level is ScopeLevel.PRIVATE
    again = await stack.promotions.request(ALICE, item.id, TEAM, Visibility.SCOPE)
    with pytest.raises(PermissionDenied):
        await stack.promotions.decide(BOB, again.id, "withdraw")
    assert (
        await stack.promotions.decide(ALICE, again.id, "withdraw")
    ).status is PromotionStatus.WITHDRAWN
    with pytest.raises(Conflict):
        await stack.promotions.decide(ROOT, again.id, "approve")  # already closed


async def test_only_other_admins_approve(stack):
    item = await note(stack)
    promo = await stack.promotions.request(ALICE, item.id, TEAM, Visibility.SCOPE)
    for who in (ALICE, BOB, Principal("ed", "org1", Role.EDITOR)):
        with pytest.raises(PermissionDenied):
            await stack.promotions.decide(who, promo.id, "approve")
    with pytest.raises(NotFound):
        await stack.promotions.decide(OUTSIDE_ADMIN, promo.id, "approve")  # other tenant
    admin_author = Principal("ann", "org1", Role.ADMIN, teams=frozenset({"t1"}))
    own = await note(stack, admin_author, title="x", body="y")
    with pytest.raises(ValidationError, match="kb_share"):
        await stack.promotions.request(admin_author, own.id, TEAM, Visibility.SCOPE)  # no need
    assert (
        await stack.promotions.decide(ROOT2, promo.id, "approve")
    ).status is PromotionStatus.APPROVED


async def test_editing_after_the_request_makes_it_stale_not_published(stack):
    item = await note(stack)
    promo = await stack.promotions.request(ALICE, item.id, TEAM, Visibility.SCOPE)
    await stack.knowledge.update(ALICE, item.id, ItemPatch(body="bait and switch"), item.revision)
    with pytest.raises(Conflict, match="changed"):
        await stack.promotions.decide(ROOT, promo.id, "approve")
    assert (await stack.backend.items.get_system("org1", item.id)).scope.level is ScopeLevel.PRIVATE
    stale = (await stack.promotions.list(ROOT, PromotionStatus.STALE))[0].promotion
    assert stale.id == promo.id


async def test_request_validation(stack):
    item = await note(stack)
    with pytest.raises(NotFound):
        await stack.promotions.request(
            CAROL, item.id, TEAM, Visibility.SCOPE
        )  # not hers: invisible
    with pytest.raises(PermissionDenied):
        await stack.promotions.request(
            ALICE, item.id, Scope(ScopeLevel.TEAM, "t9"), Visibility.SCOPE
        )  # not a member of that team
    with pytest.raises(ValidationError):
        await stack.promotions.request(ALICE, item.id, ALICE_PRIVATE_SCOPE, Visibility.ORG)
    first = await stack.promotions.request(ALICE, item.id, TEAM, Visibility.SCOPE, None)
    with pytest.raises(Conflict):
        await stack.promotions.request(ALICE, item.id, TEAM, Visibility.SCOPE)  # one at a time
    with pytest.raises(ValidationError):
        await stack.promotions.decide(ROOT, first.id, "banana")
    with pytest.raises(ValidationError):
        await stack.promotions.request(ALICE, item.id, TEAM, Visibility.SCOPE, "x" * 3000)


ALICE_PRIVATE_SCOPE = Scope(ScopeLevel.PRIVATE, "alice")


async def test_a_project_item_can_be_promoted_org_wide_and_then_appears_external(stack):
    item = await note(stack, share_scope_project())
    # alice shares to her project herself, then asks for org-wide visibility
    shared = await stack.knowledge.move(
        ALICE, item.id, Scope(ScopeLevel.PROJECT, "p1"), Visibility.SCOPE, item.revision
    )
    promo = await stack.promotions.request(
        ALICE, shared.id, shared.scope, Visibility.ORG, "everyone should know"
    )
    await stack.promotions.decide(ROOT, promo.id, "approve")
    seen = await stack.consult.consult(CAROL, ConsultRequest("radar echo"))
    assert [m.item_id for m in seen.referenced] == [item.id]  # labelled as another group's


def share_scope_project():
    return ALICE


async def test_list_shows_approvers_the_queue_with_content_and_others_only_their_own(stack):
    a, b = await note(stack, ALICE), await note(stack, BOB, title="Other", body="more words")
    pa = await stack.promotions.request(ALICE, a.id, TEAM, Visibility.SCOPE)
    bob = Principal("bob", "org1", Role.MEMBER, teams=frozenset({"t1"}), projects=frozenset({"p1"}))
    await stack.promotions.request(bob, b.id, TEAM, Visibility.SCOPE)
    queue = await stack.promotions.list(ROOT)
    assert len(queue) == 2 and all(v.item is not None for v in queue)
    assert any(v.item.body == "radar echo delay gives range" for v in queue)
    mine = await stack.promotions.list(ALICE)
    assert [v.promotion.id for v in mine] == [pa.id] and mine[0].item is None
    assert [v.promotion.id for v in await stack.promotions.list(ROOT, mine=True)] == []
    assert await stack.promotions.list(OUTSIDE_ADMIN) == []


async def test_concurrent_approvals_move_the_item_once(stack):
    item = await note(stack)
    promo = await stack.promotions.request(ALICE, item.id, TEAM, Visibility.SCOPE)
    results = await asyncio.gather(
        *(stack.promotions.decide(ROOT if i % 2 else ROOT2, promo.id, "approve") for i in range(6)),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert (await stack.backend.items.get_system("org1", item.id)).revision == item.revision + 1


async def test_a_requester_promoted_to_admin_still_cannot_approve_their_own_request(stack):
    item = await note(stack)
    promo = await stack.promotions.request(ALICE, item.id, TEAM, Visibility.SCOPE)
    now_admin = Principal("alice", "org1", Role.ADMIN, teams=frozenset({"t1"}))
    with pytest.raises(PermissionDenied, match="your own"):
        await stack.promotions.decide(now_admin, promo.id, "approve")


async def drain_apply_jobs(stack):
    """Run what the job runner would run (due jobs only)."""
    stack.backend.clock.advance(seconds=120)
    done = 0
    while job := await stack.backend.jobs.claim("w", frozenset({APPLY_JOB}), 30):
        await stack.promotions.handle_apply(job)
        assert await stack.backend.jobs.complete(job.id, "w")
        done += 1
    return done


async def test_crash_after_recording_the_approval_is_repaired_by_the_job(stack, monkeypatch):
    item = await note(stack)
    promo = await stack.promotions.request(ALICE, item.id, TEAM, Visibility.SCOPE)

    async def crash(*args, **kwargs):
        raise StorageBusy("process died here")

    monkeypatch.setattr(stack.backend.items, "relocate_system", crash)
    approved = await stack.promotions.decide(ROOT, promo.id, "approve")  # decision is recorded
    assert approved.status is PromotionStatus.APPROVED
    assert (await get_item(stack, item.id)).scope.level is ScopeLevel.PRIVATE  # but not applied yet
    monkeypatch.undo()
    assert await drain_apply_jobs(stack) == 1
    moved = await get_item(stack, item.id)
    assert moved.scope == TEAM and moved.revision == item.revision + 1
    teammate = Principal("tim", "org1", Role.MEMBER, teams=frozenset({"t1"}))
    assert (await stack.consult.consult(teammate, ConsultRequest("radar echo"))).primary


async def test_job_is_a_no_op_when_nothing_was_approved_or_the_move_already_happened(stack):
    item = await note(stack)
    promo = await stack.promotions.request(ALICE, item.id, TEAM, Visibility.SCOPE)
    await stack.backend.jobs.enqueue("org1", APPLY_JOB, {"promotion_id": promo.id})
    assert await drain_apply_jobs(stack) == 1  # crash before the decision: still pending, untouched
    assert (await get_item(stack, item.id)).scope.level is ScopeLevel.PRIVATE
    assert (await stack.promotions.list(ROOT))[0].promotion.status is PromotionStatus.PENDING
    await stack.promotions.decide(ROOT, promo.id, "approve")
    revision = (await get_item(stack, item.id)).revision
    assert await drain_apply_jobs(stack) == 1  # already applied inline: repeating changes nothing
    assert (await get_item(stack, item.id)).revision == revision
    assert (await stack.promotions.list(ROOT, PromotionStatus.APPROVED))[0].promotion.id == promo.id


async def test_item_edited_between_approval_and_repair_goes_stale_not_published(stack, monkeypatch):
    item = await note(stack)
    promo = await stack.promotions.request(ALICE, item.id, TEAM, Visibility.SCOPE)

    async def busy(*args, **kwargs):
        raise StorageBusy("down")

    monkeypatch.setattr(stack.backend.items, "relocate_system", busy)
    await stack.promotions.decide(ROOT, promo.id, "approve")
    monkeypatch.undo()
    await stack.knowledge.update(
        ALICE, item.id, ItemPatch(body="changed after approval"), item.revision
    )
    await drain_apply_jobs(stack)
    assert (await get_item(stack, item.id)).scope.level is ScopeLevel.PRIVATE
    assert (await stack.promotions.list(ROOT, PromotionStatus.STALE))[0].promotion.id == promo.id


async def get_item(stack, item_id):
    return await stack.backend.items.get_system("org1", item_id)


# ------------------------------------------------------------------ scoped approvers
T1_LEAD = Principal(
    "lead1", "org1", Role.MEMBER, teams=frozenset({"t1"}), approves=frozenset({TEAM})
)
T2 = Scope(ScopeLevel.TEAM, "t2")
BIA = Principal("bia", "org1", Role.MEMBER, teams=frozenset({"t1", "t2"}))


async def test_a_team_approver_decides_only_for_their_team(stack):
    item1, item2 = await note(stack, BIA), await note(stack, BIA, title="Other", body="sonar ping")
    to_t1 = await stack.promotions.request(BIA, item1.id, TEAM, Visibility.SCOPE)
    to_t2 = await stack.promotions.request(BIA, item2.id, T2, Visibility.SCOPE)
    queue = await stack.promotions.list(T1_LEAD)
    assert [v.promotion.id for v in queue] == [to_t1.id] and queue[0].item is not None
    with pytest.raises(PermissionDenied):
        await stack.promotions.decide(T1_LEAD, to_t2.id, "approve")
    assert (
        await stack.promotions.decide(T1_LEAD, to_t1.id, "approve")
    ).status is PromotionStatus.APPROVED
    assert (await stack.promotions.list(ROOT))[
        0
    ].promotion.id == to_t2.id  # admins still see everything
    assert [
        v.promotion.id for v in await stack.promotions.list(ROOT, PromotionStatus.APPROVED)
    ] == [to_t1.id]


async def test_scoped_approvers_cannot_approve_org_level_or_their_own_requests(stack):
    item = await note(stack, T1_LEAD)
    with pytest.raises(
        ValidationError, match="kb_share"
    ):  # they may publish to their own scope directly
        await stack.promotions.request(T1_LEAD, item.id, TEAM, Visibility.SCOPE)
    shared = await stack.knowledge.move(T1_LEAD, item.id, TEAM, Visibility.SCOPE, item.revision)
    assert shared.scope == TEAM
    other = await note(stack, T1_LEAD, title="o", body="more words")
    org_req = await stack.promotions.request(T1_LEAD, other.id, ORG, Visibility.SCOPE)
    with pytest.raises(PermissionDenied):
        await stack.promotions.decide(T1_LEAD, org_req.id, "approve")  # not their scope
    with pytest.raises(PermissionDenied):
        await stack.knowledge.move(T1_LEAD, other.id, ORG, Visibility.SCOPE, other.revision)
    assert await stack.promotions.list(T1_LEAD) == []  # org-level requests are not in their queue


async def test_viewer_with_approver_scope_gains_nothing(stack):
    item = await note(stack, BIA)
    promo = await stack.promotions.request(BIA, item.id, TEAM, Visibility.SCOPE)
    viewer = Principal("vv", "org1", Role.VIEWER, approves=frozenset({TEAM}))
    with pytest.raises(PermissionDenied):
        await stack.promotions.decide(viewer, promo.id, "approve")
    assert await stack.promotions.list(viewer) == []
