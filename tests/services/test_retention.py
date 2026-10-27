import pytest

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.item import ItemPatch, Kind
from hive_mcp.domain.retention import RetentionRule, parse_rules
from hive_mcp.domain.scope import ScopeLevel
from hive_mcp.services.models import RememberRequest
from hive_mcp.services.retention import RetentionService
from tests.conformance.factory import ALICE, BOB, P1, ROOT


async def add(stack, who, title, **kw):
    req = RememberRequest(title=title, body="body words here", **kw)
    return (await stack.knowledge.remember(who, req)).item


def service(stack, rules):
    return RetentionService(
        stack.backend.maintenance, stack.purge, parse_rules(rules), stack.backend.clock
    )


async def alive(stack, *items):
    return [(await stack.backend.items.get_system("org1", i.id)) is not None for i in items]


def test_rules_parse_and_refuse_dangerous_or_unknown_input():
    (rule,) = parse_rules([{"kind": "session", "scope": "private", "days": 90}])
    assert rule == RetentionRule(90, Kind.SESSION, ScopeLevel.PRIVATE)
    assert "session in private, older than 90 days" in rule.describe()
    for bad in (
        [{"days": 30}],  # would match everything
        [{"kind": "note"}],  # no days
        [{"kind": "note", "days": 0}],
        [{"kind": "nonsense", "days": 5}],
        [{"scope": "galaxy", "days": 5}],
        [{"kind": "note", "days": 5, "action": "archive"}],  # unknown key
        ["x"],
    ):
        with pytest.raises(ValidationError):
            parse_rules(bad)


async def test_old_items_matching_a_rule_are_purged_and_others_stay(stack):
    old_private = await add(stack, ALICE, "old private")
    old_project = await add(stack, ALICE, "old project", scope=P1)
    old_session = await add(stack, ALICE, "old session", kind=Kind.SESSION)
    stack.backend.clock.advance(days=100)
    fresh_private = await add(stack, ALICE, "fresh private")
    rules = [{"scope": "private", "days": 60}, {"kind": "session", "days": 30}]
    done = await service(stack, rules).run()
    assert list(done.values()) == [2, 0]  # the private session went with rule one already
    assert await alive(stack, old_private, old_project, old_session, fresh_private) == [
        False, True, False, True,
    ]  # fmt: skip


async def test_editing_restarts_the_clock_and_a_dry_run_changes_nothing(stack):
    note = await add(stack, ALICE, "worth keeping")
    stack.backend.clock.advance(days=100)
    await stack.knowledge.update(ALICE, note.id, ItemPatch(body="updated"), note.revision)
    stack.backend.clock.advance(days=10)
    svc = service(stack, [{"scope": "private", "days": 60}])
    assert list((await svc.run()).values()) == [0]
    stack.backend.clock.advance(days=100)
    assert list((await svc.run(dry_run=True)).values()) == [1]
    assert await alive(stack, note) == [True]
    assert list((await svc.run()).values()) == [1]
    assert await alive(stack, note) == [False]


async def test_the_budget_spreads_a_big_first_run_over_several_sweeps(stack):
    made = [await add(stack, ALICE, f"n{i}") for i in range(5)]
    stack.backend.clock.advance(days=100)
    svc = service(stack, [{"kind": "note", "days": 30}])
    assert list((await svc.run(max_items=2)).values()) == [2]
    assert list((await svc.run(max_items=2)).values()) == [2]
    assert list((await svc.run(max_items=2)).values()) == [1]
    assert await alive(stack, *made) == [False] * 5


async def test_forget_can_replace_the_person_in_the_audit_log(stack):
    note = await add(stack, BOB, "bob private")
    before = [r.event.actor_id for r in await stack.backend.audit.list("org1")]
    assert "bob" in before
    result = await stack.purge.forget_user(ROOT, "bob", dry_run=False, erase_audit=True)
    assert result.purged == [note.id] and result.audit_events >= 1
    events = await stack.backend.audit.list("org1")
    assert "bob" not in {e for r in events for e in (r.event.actor_id, r.event.target_id)}
    aliases = {r.event.actor_id for r in events if r.event.actor_id.startswith("erased-")}
    assert len(aliases) == 1  # one alias for the one person: the trail stays coherent
    forget = next(r.event for r in events if r.event.action == "user.forget")
    assert forget.details["user"] in aliases and forget.actor_id == "root"
    # without the flag the id stays
    other = await add(stack, ALICE, "alice private")
    await stack.purge.forget_user(ROOT, "alice", dry_run=False)
    assert "alice" in {r.event.actor_id for r in await stack.backend.audit.list("org1")}
    assert other.id


def test_bad_rules_in_settings_stop_the_server_at_startup():
    from hive_mcp.config import Settings

    with pytest.raises(ValueError, match="HIVE_RETENTION_RULES"):
        Settings(retention_rules=[{"days": 5}])
    assert Settings(retention_rules=[{"kind": "session", "days": 5}]).retention_rules
