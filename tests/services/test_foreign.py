"""Other groups' notes: fork, hide, and propose a change."""

from __future__ import annotations

import pytest

from hive_mcp.domain.errors import NotFound, PermissionDenied, ValidationError
from hive_mcp.domain.item import ItemPatch, Status
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.services.foreign import MAX_OPEN_PER_PROPOSER, ForeignService
from hive_mcp.services.models import RememberRequest
from tests.conformance.factory import ALICE, BOB, CAROL, ROOT

P1 = Scope(ScopeLevel.PROJECT, "p1")
P2 = Scope(ScopeLevel.PROJECT, "p2")


@pytest.fixture
def foreign(stack):
    b = stack.backend
    return ForeignService(stack.knowledge, stack.graph, b.kv, b.audit, b.clock)


async def theirs(stack, title="Radar tuning", body="use a 3 dB margin", who=ROOT):
    req = RememberRequest(title, body, scope=P2, visibility=Visibility.ORG)
    return (await stack.knowledge.remember(who, req)).item


async def test_fork_copies_into_my_scope_and_links_to_the_original(stack, foreign):
    original = await theirs(stack)
    result = await foreign.fork(ALICE, original.id)
    item = result.item
    assert (item.title, item.body) == (original.title, original.body)
    assert item.scope == Scope(ScopeLevel.PRIVATE, "alice") and item.author_id == "alice"
    assert item.source == f"fork:{original.id}@{original.revision}"
    graph = await stack.graph.neighbours(ALICE, item.id)
    assert [(e.src, e.dst, e.relation) for e in graph.edges] == [
        (item.id, original.id, "derived_from")
    ]
    again = await foreign.fork(ALICE, original.id)  # the same fork twice is one note
    assert again.item.id == item.id and not again.created


async def test_fork_into_a_project_and_of_something_invisible(stack, foreign):
    original = await theirs(stack)
    result = await foreign.fork(ALICE, original.id, P1)
    assert result.item.scope == P1
    private = (await stack.knowledge.remember(BOB, RememberRequest("Secret", "x"))).item
    with pytest.raises(NotFound):
        await foreign.fork(ALICE, private.id)


async def test_diff_upstream_shows_how_the_original_moved_on(stack, foreign):
    original = await theirs(stack)
    mine = (await foreign.fork(ALICE, original.id)).item
    same = await foreign.diff_upstream(ALICE, mine.id)
    assert same.identical and same.diff == "" and same.upstream_revision == original.revision
    await stack.knowledge.update(ROOT, original.id, ItemPatch(body="use a 6 dB margin"), 1)
    moved = await foreign.diff_upstream(ALICE, mine.id)
    assert not moved.identical and moved.upstream_revision == 2 and moved.forked_revision == 1
    assert "-use a 3 dB margin" in moved.diff and "+use a 6 dB margin" in moved.diff
    plain = (await stack.knowledge.remember(ALICE, RememberRequest("Own", "text"))).item
    with pytest.raises(ValidationError, match="not made with kb_fork"):
        await foreign.diff_upstream(ALICE, plain.id)


async def test_a_deleted_original_is_reported_not_an_error(stack, foreign):
    original = await theirs(stack)
    mine = (await foreign.fork(ALICE, original.id)).item
    await stack.purge.purge(ROOT, original.id)
    gone = await foreign.diff_upstream(ALICE, mine.id)
    assert gone.upstream_revision is None and not gone.identical


async def test_ignore_is_personal_reversible_and_only_for_other_groups_notes(stack, foreign):
    original = await theirs(stack)
    await foreign.ignore(ALICE, original.id)
    assert await foreign.ignored_ids(ALICE) == {original.id}
    assert await foreign.ignored_ids(BOB) == frozenset()  # nobody else is affected
    await foreign.ignore(ALICE, original.id)  # repeating is harmless
    await foreign.ignore(ALICE, original.id, ignored=False)
    assert await foreign.ignored_ids(ALICE) == frozenset()
    mine = (await stack.knowledge.remember(ALICE, RememberRequest("Own", "text"))).item
    with pytest.raises(ValidationError, match="other groups"):
        await foreign.ignore(ALICE, mine.id)
    shared = (await stack.knowledge.remember(BOB, RememberRequest("Team", "text", scope=P1))).item
    with pytest.raises(ValidationError, match="other groups"):
        await foreign.ignore(ALICE, shared.id)  # a project mate's note is primary, not foreign


async def test_ignored_ids_are_scoped_to_the_organisation(stack, foreign):
    original = await theirs(stack)
    await foreign.ignore(ALICE, original.id)
    other_org = Principal("alice", "org2", Role.MEMBER)
    assert await foreign.ignored_ids(other_org) == frozenset()


async def test_suggest_then_accept_applies_the_reviewed_text(stack, foreign):
    original = await theirs(stack)
    suggestion = await foreign.suggest_edit(
        ALICE, original.id, title=None, body="use a 6 dB margin", note="measured on site"
    )
    assert suggestion.status == "open" and suggestion.item_revision == 1
    assert [s.id for s in await foreign.list_suggestions(ROOT)] == [suggestion.id]
    assert await foreign.list_suggestions(CAROL) == []  # a project mate of the owner is not enough
    assert await foreign.list_suggestions(ALICE) == []  # the proposer cannot review their own
    with pytest.raises(NotFound):
        await foreign.decide_suggestion(ALICE, suggestion.id, "accept")
    done = await foreign.decide_suggestion(ROOT, suggestion.id, "accept")
    assert done.status == "accepted"
    now = (await stack.knowledge.get(ROOT, original.id)).item
    assert now.body == "use a 6 dB margin" and now.title == "Radar tuning" and now.revision == 2
    with pytest.raises(ValidationError, match="already accepted"):
        await foreign.decide_suggestion(ROOT, suggestion.id, "reject")
    assert await foreign.list_suggestions(ROOT) == []
    assert [s.status for s in await foreign.list_suggestions(ROOT, include_decided=True)] == [
        "accepted"
    ]


async def test_reject_leaves_the_note_alone(stack, foreign):
    original = await theirs(stack)
    s = await foreign.suggest_edit(ALICE, original.id, title="New", body=None, note="")
    await foreign.decide_suggestion(ROOT, s.id, "reject")
    assert (await stack.knowledge.get(ROOT, original.id)).item.title == "Radar tuning"


async def test_a_suggestion_for_an_older_revision_cannot_be_accepted(stack, foreign):
    original = await theirs(stack)
    s = await foreign.suggest_edit(ALICE, original.id, title=None, body="other", note="")
    await stack.knowledge.update(ROOT, original.id, ItemPatch(body="changed meanwhile"), 1)
    with pytest.raises(ValidationError, match="changed since"):
        await foreign.decide_suggestion(ROOT, s.id, "accept")
    assert (await stack.knowledge.get(ROOT, original.id)).item.body == "changed meanwhile"
    await foreign.decide_suggestion(ROOT, s.id, "reject")


async def test_suggestion_rules(stack, foreign):
    original = await theirs(stack)
    with pytest.raises(ValidationError, match="yourself"):
        await foreign.suggest_edit(ROOT, original.id, title="x", body=None, note="")
    with pytest.raises(ValidationError, match="new title and/or"):
        await foreign.suggest_edit(ALICE, original.id, title=None, body=None, note="")
    with pytest.raises(ValidationError, match="changes nothing"):
        await foreign.suggest_edit(ALICE, original.id, title=None, body=original.body, note="")
    with pytest.raises(ValidationError, match="at most"):
        await foreign.suggest_edit(ALICE, original.id, title="x", body=None, note="n" * 1001)
    private = (await stack.knowledge.remember(BOB, RememberRequest("Secret", "x"))).item
    with pytest.raises(NotFound):
        await foreign.suggest_edit(ALICE, private.id, title="x", body=None, note="")


async def test_secrets_in_a_suggestion_are_masked_before_they_are_stored(stack, foreign):
    original = await theirs(stack)
    s = await foreign.suggest_edit(
        ALICE, original.id, title=None, body="token xoxb-1234567890-abcdefghij here", note=""
    )
    assert "xoxb-1234567890" not in (s.body or "")
    [listed] = await foreign.list_suggestions(ROOT)
    assert "xoxb-1234567890" not in (listed.body or "")


async def test_resuggesting_replaces_and_the_number_of_open_ones_is_capped(stack, foreign):
    first = await theirs(stack, "Note 0")
    a = await foreign.suggest_edit(ALICE, first.id, title="A", body=None, note="")
    b = await foreign.suggest_edit(ALICE, first.id, title="B", body=None, note="")
    assert a.id == b.id and [s.title for s in await foreign.list_suggestions(ROOT)] == ["B"]
    for n in range(1, MAX_OPEN_PER_PROPOSER):
        other = await theirs(stack, f"Note {n}", f"body {n}")
        await foreign.suggest_edit(ALICE, other.id, title=f"T{n}", body=None, note="")
    extra = await theirs(stack, "One too many", "body")
    with pytest.raises(ValidationError, match="open suggestions"):
        await foreign.suggest_edit(ALICE, extra.id, title="T", body=None, note="")
    await foreign.suggest_edit(ALICE, first.id, title="C", body=None, note="")  # replacing is fine


async def test_only_those_who_may_edit_the_note_can_decide(stack, foreign):
    original = await theirs(stack)
    s = await foreign.suggest_edit(ALICE, original.id, title="Better", body=None, note="")
    with pytest.raises((NotFound, PermissionDenied)):
        await foreign.decide_suggestion(CAROL, s.id, "accept")  # a member, not the author
    assert (await stack.knowledge.get(ROOT, original.id)).item.title == "Radar tuning"
    other_org = Principal("root", "org2", Role.ADMIN, projects=frozenset({"p2"}))
    with pytest.raises(NotFound):
        await foreign.decide_suggestion(other_org, s.id, "accept")


async def test_archived_originals_can_still_be_forked(stack, foreign):
    original = await theirs(stack)
    await stack.knowledge.update(ROOT, original.id, ItemPatch(status=Status.ARCHIVED), 1)
    result = await foreign.fork(ALICE, original.id)
    assert result.item.status is Status.ACTIVE


async def test_forgetting_a_person_drops_their_hidden_list_and_open_suggestions(stack, foreign):
    from hive_mcp.services.purge import PurgeService

    b = stack.backend
    purge = PurgeService(
        items=b.items, maintenance=b.maintenance, text=b.text, vectors=b.vectors, edges=b.edges,
        promotions=b.promotions, blobs=b.blobs, jobs=b.jobs, audit=b.audit, clock=b.clock,
        kv=b.kv,
    )  # fmt: skip
    one, two = await theirs(stack, "One", "first"), await theirs(stack, "Two", "second")
    await foreign.ignore(ALICE, one.id)
    open_one = await foreign.suggest_edit(ALICE, one.id, title="A", body=None, note="")
    decided = await foreign.suggest_edit(ALICE, two.id, title="B", body=None, note="")
    await foreign.decide_suggestion(ROOT, decided.id, "reject")
    await foreign.ignore(BOB, one.id)  # somebody else's list stays
    dry = await purge.forget_user(ROOT, "alice", dry_run=True)
    assert dry.records == 0 and await foreign.ignored_ids(ALICE) == {
        one.id
    }  # a dry run changes nothing
    await purge.forget_user(ROOT, "alice", dry_run=False, erase_audit=True)
    assert await foreign.ignored_ids(ALICE) == frozenset()
    assert await foreign.ignored_ids(BOB) == {one.id}
    rest = await foreign.list_suggestions(ROOT, include_decided=True)
    assert [s.id for s in rest] == [decided.id] and open_one.id not in [s.id for s in rest]
    assert rest[0].proposer.startswith("erased-")  # the decided one is kept, renamed
