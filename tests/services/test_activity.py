import pytest

from hive_mcp.domain.errors import PermissionDenied, ValidationError
from hive_mcp.ops.usage import UsageMeter
from hive_mcp.services.activity import ActivityReport
from hive_mcp.services.models import RememberRequest
from tests.conformance.factory import ALICE, BOB, ROOT


def report(stack, teams=None):
    teams = teams or {}
    b = stack.backend
    return ActivityReport(b.audit, b.maintenance, b.clock, lambda u: frozenset(teams.get(u, ())))


async def add(stack, who, title):
    return (await stack.knowledge.remember(who, RememberRequest(title=title, body="body"))).item


async def test_the_meter_counts_per_person_and_flush_writes_one_event_each(stack):
    meter = UsageMeter()
    for _ in range(3):
        meter.record("org1", "alice", "kb_consult")
    meter.record("org1", "alice", "kb_get")
    meter.record("org1", "bob", "kb_consult")
    assert await meter.flush(stack.backend.audit, stack.backend.clock) == 2
    assert await meter.flush(stack.backend.audit, stack.backend.clock) == 0  # nothing new
    rows = {r.event.actor_id: r.event for r in await stack.backend.audit.list("org1")}
    assert rows["alice"].details == {"calls": "4", "tools": "kb_consult=3,kb_get=1"}
    assert rows["bob"].action == "usage.tools" and rows["bob"].details["calls"] == "1"


async def test_a_failed_flush_keeps_the_counts_for_the_next_round(stack):
    meter = UsageMeter()
    meter.record("org1", "alice", "kb_get")

    class Broken:
        async def append(self, event):
            raise OSError("disk full")

    assert await meter.flush(Broken(), stack.backend.clock) == 0
    assert await meter.flush(stack.backend.audit, stack.backend.clock) == 1


async def test_the_report_adds_up_calls_writes_storage_and_teams(stack):
    meter = UsageMeter()
    for _ in range(5):
        meter.record("org1", "alice", "kb_consult")
    meter.record("org1", "bob", "kb_get")
    await add(stack, ALICE, "one")
    await add(stack, ALICE, "two")
    await add(stack, BOB, "three")
    await meter.flush(stack.backend.audit, stack.backend.clock)
    a = await report(stack, {"alice": {"t1"}, "bob": {"t1", "t2"}}).build(ROOT, days=30)
    alice, bob = a.people
    assert (alice.user, alice.calls, alice.writes, alice.items) == ("alice", 5, 2, 2)
    assert (bob.user, bob.calls, bob.writes, bob.items) == ("bob", 1, 1, 1)
    assert a.teams == {"t1": (6, 3), "t2": (1, 1)}
    assert a.tools[0] == ("kb_consult", 5) and not a.truncated


async def test_events_outside_the_window_are_left_out_and_org_isolation_holds(stack):
    meter = UsageMeter()
    meter.record("org1", "alice", "kb_get")
    await meter.flush(stack.backend.audit, stack.backend.clock)
    stack.backend.clock.advance(days=40)
    meter.record("org1", "alice", "kb_get")
    meter.record("org2", "zed", "kb_get")
    await meter.flush(stack.backend.audit, stack.backend.clock)
    a = await report(stack).build(ROOT, days=30)
    assert [(p.user, p.calls) for p in a.people] == [("alice", 1)]  # the old event is outside
    assert "zed" not in {p.user for p in a.people}


async def test_only_admins_and_sane_windows(stack):
    r = report(stack)
    with pytest.raises(PermissionDenied):
        await r.build(ALICE)
    for days in (0, 366):
        with pytest.raises(ValidationError):
            await r.build(ROOT, days)


async def test_a_busy_window_is_cut_and_says_so(stack, monkeypatch):
    from hive_mcp.services import activity

    monkeypatch.setattr(activity, "MAX_EVENTS", 3)
    monkeypatch.setattr(activity, "PAGE", 2)
    for n in range(6):
        await add(stack, ALICE, f"n{n}")
    a = await report(stack).build(ROOT)
    assert a.truncated and 0 < a.people[0].writes < 6
