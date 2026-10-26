import pytest

from hive_mcp.domain.errors import PermissionDenied, QuotaExceeded, ValidationError
from hive_mcp.domain.item import ItemPatch, Status
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.services.admin import AdminService
from hive_mcp.services.models import RememberRequest
from hive_mcp.services.quota import QuotaGuard
from hive_mcp.services.sessions import SessionReport, SessionService
from tests.conformance.factory import ALICE, BOB, ROOT, ZOE
from tests.services.conftest import build_stack


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def stack_with(backend, clock=None, **limits):
    guard = QuotaGuard(backend.maintenance, now=clock or Clock(), **limits)
    return build_stack(backend, quota=guard)


async def add(stack, who, n, size=10):
    for i in range(n):
        await stack.knowledge.remember(who, RememberRequest(title=f"t{i}", body="x" * size))


async def test_item_quota_blocks_the_next_write_but_not_other_people(backend):
    stack = stack_with(backend, max_items=3)
    await add(stack, ALICE, 3)
    with pytest.raises(QuotaExceeded, match="item quota"):
        await add(stack, ALICE, 1)
    await add(stack, BOB, 1)  # the limit is per person
    await add(stack, ROOT, 5)  # admins are exempt


async def test_character_quota_counts_title_and_body(backend):
    stack = stack_with(backend, max_chars=100)
    await stack.knowledge.remember(ALICE, RememberRequest(title="abcde", body="x" * 80))  # 85 chars
    with pytest.raises(QuotaExceeded, match="storage quota"):
        await stack.knowledge.remember(
            ALICE, RememberRequest(title="abcde", body="y" * 20)
        )  # would be 110
    await stack.knowledge.remember(ALICE, RememberRequest(title="ab", body="z" * 10))  # 97: fits


async def test_archiving_frees_quota_after_the_cache_expires(backend):
    clock = Clock()
    stack = stack_with(backend, clock, max_items=2)
    first = (await stack.knowledge.remember(ALICE, RememberRequest(title="a", body="bb"))).item
    await stack.knowledge.remember(ALICE, RememberRequest(title="c", body="dd"))
    with pytest.raises(QuotaExceeded):
        await stack.knowledge.remember(ALICE, RememberRequest(title="e", body="ff"))
    await stack.knowledge.update(ALICE, first.id, ItemPatch(status=Status.ARCHIVED), first.revision)
    with pytest.raises(QuotaExceeded):  # usage is cached for a short time
        await stack.knowledge.remember(ALICE, RememberRequest(title="e", body="ff"))
    clock.t += 31
    await stack.knowledge.remember(ALICE, RememberRequest(title="e", body="ff"))


async def test_a_single_process_cannot_overshoot_within_the_cache_window(backend):
    stack = stack_with(backend, max_items=5)
    with pytest.raises(QuotaExceeded):
        await add(stack, ALICE, 10)
    assert (await backend.maintenance.usage("org1", "alice"))[0].items == 5


async def test_reports_and_replays(backend):
    stack = stack_with(backend, max_items=1)
    sessions = SessionService(stack.knowledge, backend.blobs)
    await sessions.report_session(ALICE, SessionReport(summary="one", idempotency_key="k"))
    with pytest.raises(QuotaExceeded):
        await sessions.report_session(ALICE, SessionReport(summary="two", idempotency_key="k2"))


async def test_no_limits_means_no_queries(backend):
    guard = QuotaGuard(backend.maintenance)
    assert not guard.enabled
    await guard.check(ALICE, 10**9)  # never raises, never touches storage


async def test_admin_views_are_admin_only_and_org_scoped(stack):
    await add(stack, ALICE, 2, size=50)
    await add(stack, BOB, 1, size=5)
    await stack.knowledge.remember(ALICE, RememberRequest(title="t", body="b"))
    admin = AdminService(stack.backend.maintenance, stack.backend.audit)
    for who in (ALICE, Principal("ed", "org1", Role.EDITOR)):
        with pytest.raises(PermissionDenied):
            await admin.usage(who)
        with pytest.raises(PermissionDenied):
            await admin.audit(who)
    usage = await admin.usage(ROOT)
    assert [(u.author_id, u.items) for u in usage] == [("alice", 3), ("bob", 1)]
    assert await admin.usage(ZOE) == []
    events = await admin.audit(ROOT, action="item.create", actor="bob")
    assert [r.event.actor_id for r in events] == ["bob"]
    page = await admin.audit(ROOT, limit=2)
    rest = await admin.audit(ROOT, after_seq=page[-1].seq, limit=100)
    assert len(page) == 2 and all(r.seq > page[-1].seq for r in rest)
    assert await admin.audit(ZOE) == []
    for bad in ({"limit": 0}, {"limit": 501}, {"after_seq": -1}):
        with pytest.raises(ValidationError):
            await admin.audit(ROOT, **bad)
