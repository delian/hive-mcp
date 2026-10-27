import pytest

from hive_mcp.domain.errors import PermissionDenied, ValidationError
from hive_mcp.domain.item import ItemPatch, Kind, Status
from hive_mcp.services.digest import DigestService, render_text
from hive_mcp.services.models import RememberRequest
from hive_mcp.services.staleness import StaleReview
from tests.conformance.factory import ALICE, BOB, P1


@pytest.fixture
def review(stack):
    b = stack.backend
    return StaleReview(b.items, b.kv, b.audit, b.clock)


async def add(stack, who, title, **kw):
    req = RememberRequest(title=title, body="body text", **kw)
    return (await stack.knowledge.remember(who, req)).item


async def test_old_untouched_notes_are_stale_and_fresh_or_logs_are_not(stack, review):
    old = await add(stack, ALICE, "old note")
    log = await add(stack, ALICE, "a session", kind=Kind.SESSION)
    stack.backend.clock.advance(days=200)
    fresh = await add(stack, ALICE, "fresh note")
    page = await review.stale(ALICE, days=180)
    assert [s.item.id for s in page.items] == [old.id]
    assert fresh.id not in [s.item.id for s in page.items] and log.id not in str(page.items)
    assert page.next_cursor is None
    # an edit restarts the clock
    await stack.knowledge.update(ALICE, old.id, ItemPatch(body="revised"), old.revision)
    assert (await review.stale(ALICE, days=180)).items == []


async def test_confirming_resets_the_clock_without_a_new_revision(stack, review):
    note = await add(stack, ALICE, "still good")
    stack.backend.clock.advance(days=200)
    at = await review.confirm(ALICE, note.id)
    assert (await review.stale(ALICE, days=180)).items == []
    assert (await stack.knowledge.get(ALICE, note.id)).item.revision == note.revision
    stack.backend.clock.advance(days=100)
    (again,) = (await review.stale(ALICE, days=90)).items
    assert again.confirmed_by == ALICE.id and again.last_touched == at


async def test_only_editors_confirm_and_only_active_items(stack, review):
    note = await add(stack, ALICE, "shared", scope=P1)
    with pytest.raises(PermissionDenied):
        await review.confirm(BOB, note.id)  # a project member, but not an editor of Alice's note
    await stack.knowledge.update(ALICE, note.id, ItemPatch(status=Status.ARCHIVED), note.revision)
    with pytest.raises(ValidationError):
        await review.confirm(ALICE, note.id)


async def test_mine_only_scopes_to_the_author_and_curators_see_what_they_can_edit(stack, review):
    mine = await add(stack, ALICE, "mine", scope=P1)
    stack.backend.clock.advance(days=300)
    assert [s.item.id for s in (await review.stale(ALICE)).items] == [mine.id]
    assert (await review.stale(BOB)).items == []
    assert (await review.stale(BOB, mine_only=False)).items == []  # cannot edit Alice's note


async def test_paging_and_validation(stack, review):
    ids = [(await add(stack, ALICE, f"n{i}")).id for i in range(3)]
    stack.backend.clock.advance(days=400)
    first = await review.stale(ALICE, limit=2)
    assert len(first.items) == 2 and first.next_cursor
    rest = await review.stale(ALICE, limit=2, after=first.next_cursor)
    assert sorted(s.item.id for s in [*first.items, *rest.items]) == sorted(ids)
    assert rest.next_cursor is None
    for kw in ({"days": 1}, {"days": 4000}, {"limit": 0}):
        with pytest.raises(ValidationError):
            await review.stale(ALICE, **kw)


async def test_the_digest_asks_the_author_when_enabled(stack, review):
    note = await add(stack, ALICE, "Old runbook")
    stack.backend.clock.advance(days=200)
    on = DigestService(stack.knowledge, stack.promotions, stack.backend.clock, review, 180)
    off = DigestService(stack.knowledge, stack.promotions, stack.backend.clock)
    digest = await on.build(ALICE)
    assert [i.id for i in digest.stale] == [note.id] and not digest.empty
    assert "Still true?" in render_text(digest) and "Old runbook" in render_text(digest)
    assert (await off.build(ALICE)).stale == () and (await off.build(ALICE)).empty
    assert (await on.build(BOB)).stale == ()
