"""The digest: what it includes and leaves out, who may see what, and the weekly email."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hive_mcp.domain.errors import NotifyError, ValidationError
from hive_mcp.domain.item import Kind
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.security.directory import DirectoryUser, UserDirectory
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.services import digest as digest_module
from hive_mcp.services.digest import DigestSender, DigestService, render_text
from hive_mcp.services.models import RememberRequest
from tests.conformance.factory import ALICE, BOB, CAROL, P1, P2, ROOT

if TYPE_CHECKING:
    from hive_mcp.ports.notify import Notification


def person(who: Principal) -> Principal:
    """The same person as the user directory knows them: by email address."""
    return Principal(f"{who.id}@acme.com", who.org_id, Role.MEMBER, who.teams, who.projects)


TEAM_T1 = Scope(ScopeLevel.TEAM, "t1")


@pytest.fixture
def digests(stack):
    return DigestService(stack.knowledge, stack.promotions, stack.backend.clock)


async def add(stack, who, title, scope=None, kind=Kind.NOTE, tags=(), visibility=Visibility.SCOPE):  # noqa: PLR0913, PLR0917
    return (
        await stack.knowledge.remember(
            who,
            RememberRequest(
                title, "body text", kind=kind, tags=tags, scope=scope, visibility=visibility
            ),
        )
    ).item


async def test_new_items_from_others_in_my_places_grouped_and_attributed(stack, digests):
    mine = await add(stack, BOB, "Bob's finding", P1, tags=("domain/rf",))
    await add(stack, ALICE, "Alice's own note", P1)  # her own: she knows
    await add(stack, BOB, "Bob's private thought")  # private to Bob
    await add(stack, CAROL, "Carol's project p2", P2)  # a project Alice is not in
    digest = await digests.build(ALICE)
    assert [i.id for i in digest.items] == [mine.id] and digest.total_new == 1
    assert digest.items[0].author == "bob" and digest.items[0].origin == "project p1"
    assert digest.top_tags == (("domain/rf", 1),) and digest.by_origin == {"project p1": 1}
    assert not digest.empty and digest.scanned_all


async def test_only_the_window_counts(stack, digests):
    await add(stack, BOB, "Old news", P1)
    stack.backend.clock.advance(days=10)
    fresh = await add(stack, BOB, "This week", P1)
    assert [i.id for i in (await digests.build(ALICE, days=7)).items] == [fresh.id]
    assert len((await digests.build(ALICE, days=30)).items) == 2


async def test_personal_logs_are_not_news(stack, digests):
    await add(stack, BOB, "A session log", P1, kind=Kind.SESSION)
    await add(stack, BOB, "A compaction", P1, kind=Kind.COMPACTION)
    assert (await digests.build(ALICE)).items == ()


async def test_other_groups_knowledge_is_shown_only_when_asked_and_is_labelled(stack, digests):
    item = await add(stack, CAROL, "Shared by carol", P2)
    shared = await stack.backend.items.relocate_system(
        "org1", item.id, P2, Visibility.ORG, item.revision
    )
    assert (await digests.build(ALICE)).items == ()
    digest = await digests.build(ALICE, include_external=True)
    assert [i.id for i in digest.items] == [shared.id] and digest.items[0].external
    assert digest.items[0].origin.startswith("EXTERNAL:")


async def test_the_list_is_cut_at_the_limit_but_the_count_is_honest(stack, digests):
    for n in range(5):
        await add(stack, BOB, f"Item {n}", P1)
    digest = await digests.build(ALICE, limit=2)
    assert len(digest.items) == 2 and digest.total_new == 5
    assert "and 3 more" in render_text(digest)


async def test_approvers_see_the_pending_requests_and_others_do_not(stack, digests):
    item = await add(stack, ALICE, "Worth sharing", P1)
    await stack.promotions.request(ALICE, item.id, TEAM_T1, Visibility.SCOPE, "please")
    digest = await digests.build(ROOT)
    assert digest.pending_approvals == 1 and not digest.empty
    assert "1 promotion request(s) wait for your decision" in render_text(digest)
    assert (await digests.build(BOB)).pending_approvals == 0
    assert (await digests.build(ALICE)).pending_approvals == 0  # the requester is not an approver


async def test_scanning_is_bounded_on_a_very_busy_knowledge_base(stack, digests, monkeypatch):
    monkeypatch.setattr(digest_module, "PAGE", 2)
    monkeypatch.setattr(digest_module, "MAX_SCANNED", 4)
    for n in range(7):
        await add(stack, BOB, f"Busy {n}", P1)
    digest = await digests.build(ALICE)
    assert not digest.scanned_all and 4 <= digest.total_new < 7
    assert "Very busy week" in render_text(digest)


async def test_arguments_are_validated(stack, digests):
    for kw in ({"days": 0}, {"days": 91}, {"limit": 0}, {"limit": 101}):
        with pytest.raises(ValidationError):
            await digests.build(ALICE, **kw)


async def test_the_text_lists_titles_and_origins_but_never_bodies(stack, digests):
    await add(stack, BOB, "Dwell time plan", P1, tags=("domain/rf",))
    text = render_text(await digests.build(ALICE), "alice@acme.com")
    assert (
        "for alice@acme.com" in text and "project p1 (1 new)" in text and "Dwell time plan" in text
    )
    assert "body text" not in text and "kb_get" in text


# ---------------------------------------------------------------- the weekly email
class Outbox:
    def __init__(self):
        self.sent: list[Notification] = []
        self.fail = False

    async def send(self, notification):
        if self.fail:
            raise NotifyError("down")
        self.sent.append(notification)


@pytest.fixture
def mailing(stack, digests, tmp_path):
    directory = UserDirectory(AtomicJsonFile(tmp_path / "users.json", default=[]))
    for who in (ALICE, BOB, CAROL):
        directory.upsert(
            DirectoryUser(f"{who.id}@acme.com", who.org_id, Role.MEMBER, who.teams, who.projects)
        )
    outbox = Outbox()

    def make():
        return DigestSender(digests, directory, outbox, stack.backend.kv, stack.backend.clock)

    return make, outbox, directory


async def test_each_person_gets_their_own_digest_once_a_week(stack, mailing):
    make, outbox, _ = mailing
    await add(stack, person(BOB), "News for project one", P1)
    sender = make()
    assert await sender.send_all() == (1, 0)  # alice has news; bob wrote it; carol is elsewhere
    (note,) = outbox.sent
    assert note.recipients == ("alice@acme.com",) and "News for project one" in note.text
    assert await sender.send_all() == (0, 0)  # already sent this week
    assert len(outbox.sent) == 1
    stack.backend.clock.advance(days=8)
    await add(stack, person(BOB), "Next week", P1)
    assert await sender.send_all() == (1, 0)  # a new week, new claims, new news
    assert (
        "Next week" in outbox.sent[-1].text and "News for project one" not in outbox.sent[-1].text
    )


async def test_two_replicas_do_not_double_send(stack, mailing):
    make, outbox, _ = mailing
    await add(stack, person(BOB), "Shared news", P1)
    import asyncio

    await asyncio.gather(make().send_all(), make().send_all())
    assert [n.recipients for n in outbox.sent].count(("alice@acme.com",)) <= 1


async def test_disabled_people_and_empty_digests_get_nothing(stack, mailing):
    make, outbox, directory = mailing
    await add(stack, person(BOB), "News", P1)
    directory.upsert(
        DirectoryUser(
            "alice@acme.com", "org1", Role.MEMBER, projects=frozenset({"p1"}), disabled=True
        )
    )
    assert await make().send_all() == (0, 0) and outbox.sent == []


async def test_a_failed_send_is_retried_at_the_next_round(stack, mailing):
    make, outbox, _ = mailing
    await add(stack, person(BOB), "News", P1)
    sender = make()
    outbox.fail = True
    assert await sender.send_all() == (0, 1)
    outbox.fail = False
    assert await sender.send_all() == (1, 0)  # the claim was released, so it is tried again
    assert [n.recipients for n in outbox.sent] == [("alice@acme.com",)]


# ---------------------------------------------------------------- items changed by others
async def edit(stack, who, item, title):
    from hive_mcp.domain.item import ItemPatch

    return await stack.knowledge.update(who, item.id, ItemPatch(title=title), item.revision)


async def test_old_items_edited_by_someone_else_are_listed_with_the_editor(stack, digests):
    from hive_mcp.domain.principal import Role

    editor = Principal("eddie", "org1", Role.EDITOR, projects=frozenset({"p1"}))
    old = await add(stack, BOB, "Dwell plan", P1)
    stack.backend.clock.advance(days=20)
    await edit(stack, editor, old, "Dwell plan v2")
    digest = await digests.build(ALICE)
    assert [(i.id, i.title, i.edited_by) for i in digest.edited] == [
        (old.id, "Dwell plan v2", "eddie")
    ]
    assert digest.items == () and digest.total_edited == 1 and not digest.empty
    text = render_text(digest)
    assert "Changed by others (1)" in text and "edited by eddie" in text


async def test_what_i_changed_myself_and_brand_new_items_are_not_repeated_as_edits(stack, digests):
    mine = await add(stack, BOB, "Bob's note", P1)
    stack.backend.clock.advance(days=20)
    await edit(stack, BOB, mine, "Bob's note, revised")  # alice is the reader: bob edits his own
    fresh = await add(stack, BOB, "Fresh", P1)
    await edit(
        stack, BOB, fresh, "Fresh, revised"
    )  # new this week: it is under "new", not "edited"
    digest = await digests.build(ALICE)
    assert [i.id for i in digest.items] == [fresh.id]
    assert [i.id for i in digest.edited] == [
        mine.id
    ]  # an edit by the author counts for other readers
    own = await digests.build(BOB)
    assert own.edited == () and own.items == ()  # bob does not hear about his own edits


async def test_edits_outside_the_window_or_in_unreadable_places_are_ignored(stack, digests):
    quiet = await add(stack, BOB, "Quiet", P1)
    other = await add(stack, CAROL, "Elsewhere", P2)
    stack.backend.clock.advance(days=20)
    await edit(stack, CAROL, other, "Elsewhere v2")  # a project alice is not in
    await edit(stack, BOB, quiet, "Quiet, but long ago")
    stack.backend.clock.advance(days=20)
    assert (await digests.build(ALICE, days=7)).edited == ()  # both edits are older than a week now
    wide = await digests.build(
        ALICE, days=30
    )  # the edits (20 days ago) are inside, the creations (40) are not
    assert [i.id for i in wide.edited] == [quiet.id]  # carol's project stays invisible to alice


async def test_the_edited_list_respects_the_limit_but_counts_everything(stack, digests):
    items = [await add(stack, BOB, f"Old {n}", P1) for n in range(4)]
    stack.backend.clock.advance(days=20)
    for item in items:
        await edit(stack, BOB, item, item.title + "!")
    digest = await digests.build(ALICE, limit=2)
    assert len(digest.edited) == 2 and digest.total_edited == 4
