import pytest

from hive_mcp.domain.access import read_filter
from hive_mcp.domain.errors import NotFound, PermissionDenied
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.ports.blobs import BlobRef
from hive_mcp.services.models import ConsultRequest, RememberRequest
from hive_mcp.services.purge import PURGE_JOB
from hive_mcp.services.sessions import SessionReport
from tests.conformance.factory import ALICE, BOB, CAROL, EDDIE, ROOT, ZOE

P1 = Scope(ScopeLevel.PROJECT, "p1")
TEAM = Scope(ScopeLevel.TEAM, "t1")


async def note(stack, who, title="Pulse timing", body="radar echo delay", scope=None):
    return (
        await stack.knowledge.remember(who, RememberRequest(title=title, body=body, scope=scope))
    ).item


async def gone_everywhere(stack, item_id, query):
    assert await stack.backend.items.get_system("org1", item_id) is None
    flt_hits = await stack.consult.consult(ALICE, ConsultRequest(query))
    assert item_id not in [m.item_id for m in flt_hits.primary + flt_hits.referenced]
    text_hits = await stack.backend.text.search(read_filter(ALICE), query, 10)
    assert item_id not in [h.item_id for h in text_hits]  # not just hidden: really removed
    assert await stack.backend.edges.incident("org1", [item_id]) == []


async def test_author_purges_everything_attached(stack):
    keep = await note(stack, ALICE, "Keeper", "unrelated words")
    doomed = await note(stack, ALICE)
    await stack.graph.link(ALICE, doomed.id, keep.id, "derived_from")
    await stack.curation.retag(ALICE, [doomed.id], ["x"], [])
    vector = await stack.embedder.embed_query("radar echo delay")
    assert await stack.backend.vectors.search(
        read_filter(ALICE), stack.embedder.model_id, vector, 5
    )
    purged = await stack.purge.purge(ALICE, doomed.id)
    assert purged.id == doomed.id
    await gone_everywhere(stack, doomed.id, "radar echo delay")
    after = await stack.backend.vectors.search(
        read_filter(ALICE), stack.embedder.model_id, vector, 5
    )
    assert doomed.id not in [h.item_id for h in after]
    assert await stack.backend.items.tag_counts(ALICE) == []  # tag "x" died with it
    assert (
        await stack.backend.items.get_system("org1", keep.id)
    ).id == keep.id  # neighbours survive
    assert "item.purge" in [r.event.action for r in await stack.backend.audit.list("org1")]
    with pytest.raises(NotFound):
        await stack.purge.purge(ALICE, doomed.id)  # already gone


async def test_who_may_purge(stack):
    shared = await note(stack, ALICE, scope=P1)
    for who, error in [
        (BOB, PermissionDenied),
        (EDDIE, PermissionDenied),
        (CAROL, NotFound),
        (ZOE, NotFound),
    ]:
        with pytest.raises(error):
            await stack.purge.purge(
                who, shared.id
            )  # not author/admin; other groups cannot even see it
    assert await stack.backend.items.get_system("org1", shared.id)
    await stack.purge.purge(ROOT, shared.id)  # an admin may, even outside the project
    assert await stack.backend.items.get_system("org1", shared.id) is None
    private = await note(stack, ALICE)
    with pytest.raises(NotFound):
        await stack.purge.purge(BOB, private.id)  # private to alice: not even visible
    left_project = Principal("alice", "org1", Role.MEMBER)  # author, but no longer in the project
    other = await note(stack, ALICE, scope=P1)
    with pytest.raises(NotFound):
        await stack.purge.purge(left_project, other.id)  # can no longer even see it: ask an admin


async def test_stored_logs_are_deleted_only_when_nothing_else_uses_them(stack):
    report = SessionReport(summary="did things", log="the full log text", idempotency_key="s1")
    one = (await stack.sessions.report_session(ALICE, report)).item
    ref = BlobRef("org1", one.source.split(":")[1], int(one.source.split(":")[2]))
    two = (
        await stack.sessions.report_session(
            ALICE, SessionReport(summary="again", log="the full log text", idempotency_key="s2")
        )
    ).item
    assert one.source == two.source  # content-addressed: the same bytes are stored once
    await stack.purge.purge(ALICE, one.id)
    assert await stack.backend.blobs.exists(ref)  # still needed by the second report
    await stack.purge.purge(ALICE, two.id)
    assert not await stack.backend.blobs.exists(ref)


async def test_purge_also_removes_promotion_requests_and_idempotency_keys(stack):
    member = Principal("bia", "org1", Role.MEMBER, teams=frozenset({"t1"}))
    item = (
        await stack.knowledge.remember(
            member, RememberRequest(title="t", body="b b", idempotency_key="k")
        )
    ).item
    await stack.promotions.request(
        member, item.id, TEAM, __import__("hive_mcp.domain.scope", fromlist=["x"]).Visibility.SCOPE
    )
    await stack.purge.purge(member, item.id)
    assert await stack.backend.promotions.list("org1") == []
    again = await stack.knowledge.remember(
        member, RememberRequest(title="t", body="b b", idempotency_key="k")
    )
    assert again.created and again.item.id != item.id  # the key is free again


async def test_interrupted_purge_is_finished_by_the_cleanup_job(stack, monkeypatch):
    doomed = await note(stack, ALICE)

    async def die(*args, **kwargs):
        raise RuntimeError("process died after deleting the row")

    monkeypatch.setattr(stack.purge, "_cleanup", die)
    with pytest.raises(RuntimeError):
        await stack.purge.purge(ALICE, doomed.id)
    monkeypatch.undo()
    assert (
        await stack.backend.items.get_system("org1", doomed.id) is None
    )  # row gone, leftovers remain
    stack.backend.clock.advance(seconds=120)
    job = await stack.backend.jobs.claim("w", frozenset({PURGE_JOB}), 30)
    assert job
    await stack.purge.handle_cleanup(job)
    await gone_everywhere(stack, doomed.id, "radar echo delay")


async def test_cleanup_job_does_nothing_if_the_purge_never_happened(stack):
    alive = await note(stack, ALICE)
    await stack.backend.jobs.enqueue("org1", PURGE_JOB, {"item_id": alive.id, "source": ""})
    job = await stack.backend.jobs.claim("w", frozenset({PURGE_JOB}), 30)
    await stack.purge.handle_cleanup(job)
    assert (
        await stack.consult.consult(ALICE, ConsultRequest("radar echo"))
    ).primary  # still searchable


# ------------------------------------------------------------------ forget a user
async def test_forget_user_erases_private_items_and_lists_shared_ones(stack):
    mine = [await note(stack, ALICE, f"p{i}", f"private words {i}") for i in range(3)]
    shared = await note(stack, ALICE, "shared one", "shared words", scope=P1)
    bobs = await note(stack, BOB, "bobs", "other words")
    with pytest.raises(PermissionDenied):
        await stack.purge.forget_user(EDDIE, "alice")
    dry = await stack.purge.forget_user(ROOT, "alice")
    assert sorted(dry.purged) == sorted(i.id for i in mine) and dry.kept_shared == [shared.id]
    assert await stack.backend.items.get_system("org1", mine[0].id)  # dry run changed nothing
    done = await stack.purge.forget_user(ROOT, "alice", dry_run=False)
    assert sorted(done.purged) == sorted(i.id for i in mine)
    assert [await stack.backend.items.get_system("org1", i.id) for i in mine] == [None] * 3
    assert await stack.backend.items.get_system(
        "org1", shared.id
    ) and await stack.backend.items.get_system("org1", bobs.id)
    everything = await stack.purge.forget_user(ROOT, "alice", include_shared=True, dry_run=False)
    assert everything.purged == [shared.id]
    assert await stack.backend.items.get_system(
        "org1", bobs.id
    )  # other people's items are never touched
    assert "user.forget" in [r.event.action for r in await stack.backend.audit.list("org1")]
    assert (
        await stack.purge.forget_user(ZOE, "bob")
    ).purged == []  # another org's admin sees nothing of ours


# ------------------------------------------------------------------ retention
async def test_retention_sweeps_old_sessions_and_compactions_only(stack):
    clock = stack.backend.clock
    old_session = (
        await stack.sessions.report_session(
            ALICE, SessionReport(summary="old session", idempotency_key="a")
        )
    ).item
    old_note = await note(stack, ALICE, "old but valuable", "knowledge stays")
    old_compaction = (
        await stack.sessions.report_compaction(
            BOB, SessionReport(summary="old compaction", idempotency_key="b")
        )
    ).item
    clock.advance(days=40)
    fresh = (
        await stack.sessions.report_session(
            ALICE, SessionReport(summary="fresh session", idempotency_key="c")
        )
    ).item
    assert await stack.purge.sweep(0) == 0  # disabled
    assert await stack.purge.sweep(30) == 2
    ids = {
        i: await stack.backend.items.get_system("org1", i)
        for i in (old_session.id, old_compaction.id, old_note.id, fresh.id)
    }
    assert ids[old_session.id] is None and ids[old_compaction.id] is None
    assert ids[old_note.id] is not None and ids[fresh.id] is not None
    assert await stack.purge.sweep(30) == 0  # nothing left to do
    assert [
        r.event.actor_id
        for r in await stack.backend.audit.list("org1")
        if r.event.action == "item.purge"
    ] == ["system:retention"] * 2


async def test_retention_respects_the_per_run_limit(stack):
    for i in range(5):
        await stack.sessions.report_session(
            ALICE, SessionReport(summary=f"s{i}", idempotency_key=f"k{i}")
        )
    stack.backend.clock.advance(days=40)
    assert await stack.purge.sweep(30, max_items=3) == 3
    assert await stack.purge.sweep(30, max_items=3) == 2
