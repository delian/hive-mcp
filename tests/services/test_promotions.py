import asyncio

import pytest

from hive_mcp.domain.errors import Conflict, NotFound, PermissionDenied, ValidationError
from hive_mcp.domain.item import ItemPatch
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.promotion import PromotionStatus
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.services.models import ConsultRequest, RememberRequest
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
