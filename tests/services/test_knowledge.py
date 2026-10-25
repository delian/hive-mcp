import asyncio

import pytest

from hive_mcp.domain.access import Tier, read_filter
from hive_mcp.domain.errors import (
    EmbeddingUnavailable,
    IdempotencyConflict,
    NotFound,
    PermissionDenied,
)
from hive_mcp.domain.item import ItemPatch, Kind, Status
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.embeddings.hashing import HashingEmbedder
from hive_mcp.ports.indexes import TextDoc
from hive_mcp.services.indexing import INDEX_JOB
from hive_mcp.services.models import ConsultRequest, ForeignPolicy, RememberRequest
from tests.conformance.factory import ALICE, BOB, CAROL, P1, P2, ROOT
from tests.services.conftest import build_stack


def req(
    title="Radar range equation",
    body="Range follows from pulse delay and the speed of light.",
    **kw,
):
    return RememberRequest(title=title, body=body, **kw)


async def ids(stack, who, query, **kw):
    result = await stack.consult.consult(who, ConsultRequest(query, **kw))
    return [m.item_id for m in result.primary], [m.item_id for m in result.referenced]


async def test_remember_is_private_by_default_and_findable_by_its_owner_only(stack):
    saved = await stack.knowledge.remember(ALICE, req())
    assert (
        saved.item.scope == Scope(ScopeLevel.PRIVATE, "alice") and saved.created and saved.indexed
    )
    assert await ids(stack, ALICE, "radar range") == ([saved.item.id], [])
    assert await ids(stack, BOB, "radar range") == ([], [])  # same project, still private
    assert await ids(stack, CAROL, "radar range") == ([], [])


async def test_consult_labels_where_results_come_from(stack):
    mine = await stack.knowledge.remember(ALICE, req())
    result = await stack.consult.consult(ALICE, ConsultRequest("pulse delay"))
    match = result.primary[0]
    assert match.item_id == mine.item.id and match.provenance == "your private notes"
    assert match.tier is Tier.PRIMARY and "pulse delay" in match.snippet


async def test_secrets_are_masked_before_storage_and_reported(stack):
    secret = "ghp_" + "Ab1Cd2Ef3G" * 4
    saved = await stack.knowledge.remember(
        ALICE, req(body=f"Deploy with token {secret} then verify.")
    )
    assert saved.redactions == {"github-token": 1}
    assert secret not in saved.item.body and "[REDACTED:github-token]" in saved.item.body
    stored = await stack.backend.items.get(ALICE, saved.item.id)
    assert secret not in stored.item.body
    assert await ids(stack, ALICE, secret) == ([], [])  # the secret itself is not searchable


@pytest.mark.parametrize(
    ("who", "scope", "vis"),
    [
        (ALICE, Scope(ScopeLevel.TEAM, "t1"), Visibility.SCOPE),
        (ALICE, Scope(ScopeLevel.ORG, "org1"), Visibility.SCOPE),
        (ALICE, P1, Visibility.ORG),  # publishing beyond the project needs an approver
        (BOB, Scope(ScopeLevel.PROJECT, "p1"), Visibility.ORG),
    ],
)
async def test_wide_sharing_needs_an_approver(stack, who, scope, vis):
    with pytest.raises(PermissionDenied, match="approval"):
        await stack.knowledge.remember(who, req(scope=scope, visibility=vis))


async def test_project_sharing_is_self_service_for_members_only(stack):
    shared = await stack.knowledge.remember(ALICE, req(scope=P1))
    assert await ids(stack, BOB, "radar") == ([shared.item.id], [])
    with pytest.raises(PermissionDenied):
        await stack.knowledge.remember(ALICE, req(scope=P2))  # alice is not in p2
    assert (
        await stack.knowledge.remember(
            ROOT, req(scope=Scope(ScopeLevel.ORG, "org1"), visibility=Visibility.ORG)
        )
    ).created


async def test_idempotent_remember_does_not_duplicate_or_reindex(stack):
    first = await stack.knowledge.remember(ALICE, req(idempotency_key="k"))
    again = await stack.knowledge.remember(ALICE, req(idempotency_key="k"))
    assert first.created and not again.created and again.item.id == first.item.id
    assert len((await ids(stack, ALICE, "radar"))[0]) == 1
    with pytest.raises(IdempotencyConflict):
        await stack.knowledge.remember(ALICE, req(title="Other", idempotency_key="k"))


async def test_concurrent_remember_with_one_key_stores_once(stack):
    results = await asyncio.gather(
        *(stack.knowledge.remember(ALICE, req(idempotency_key="same")) for _ in range(6))
    )
    assert sum(r.created for r in results) == 1 and len({r.item.id for r in results}) == 1


async def foreign_setup(stack):
    """Carol's project (p2) published an org-visible item; Alice has no matching knowledge."""
    published = await stack.knowledge.remember(
        ROOT,
        req(
            title="Antenna gain table",
            body="Gain figures for the X-band antenna.",
            scope=P2,
            visibility=Visibility.ORG,
        ),
    )
    return published.item


async def test_foreign_results_are_labelled_and_policy_controlled(stack):
    foreign = await foreign_setup(stack)
    primary, referenced = await ids(
        stack, ALICE, "antenna gain"
    )  # AUTO: alice has nothing of her own
    assert primary == [] and referenced == [foreign.id]
    result = await stack.consult.consult(ALICE, ConsultRequest("antenna gain"))
    match = result.referenced[0]
    assert match.tier is Tier.REFERENCED and match.provenance.startswith("EXTERNAL")
    assert "project p2" in match.provenance and "root" in match.provenance
    assert any("other groups" in note for note in result.notes)
    assert await ids(stack, ALICE, "antenna gain", include_foreign=ForeignPolicy.NEVER) == ([], [])
    assert (await ids(stack, ALICE, "antenna gain", include_foreign=ForeignPolicy.ALWAYS))[1] == [
        foreign.id
    ]


async def test_auto_hides_foreign_results_when_your_own_knowledge_is_enough(stack):
    await foreign_setup(stack)
    for i in range(3):
        await stack.knowledge.remember(
            ALICE, req(title=f"My antenna note {i}", body="antenna gain thoughts")
        )
    primary, referenced = await ids(stack, ALICE, "antenna gain")
    assert len(primary) == 3 and referenced == []
    assert (await ids(stack, ALICE, "antenna gain", include_foreign=ForeignPolicy.ALWAYS))[1] != []


async def test_consult_rechecks_access_even_if_an_index_is_wrong(stack):
    secret = await stack.knowledge.remember(
        CAROL, req(title="Carol private", body="zebra zebra zebra")
    )
    # Simulate a stale/buggy index that exposes Carol's private item to everyone in the org.
    wide_open = secret.item.acl.__class__(
        secret.item.org_id, Scope(ScopeLevel.ORG, "org1"), Visibility.ORG
    )
    await stack.backend.text.upsert(TextDoc(secret.item.id, wide_open, "zebra zebra zebra"))
    assert [h.item_id for h in await stack.backend.text.search(read_filter(ALICE), "zebra", 5)] == [
        secret.item.id
    ]
    assert await ids(stack, ALICE, "zebra") == ([], [])  # the repository still says no


async def test_promotion_updates_both_indexes_and_demotion_hides_again(stack):
    saved = await stack.knowledge.remember(ALICE, req())
    moved = await stack.knowledge.move(ALICE, saved.item.id, P1, Visibility.SCOPE, 1)
    assert await ids(stack, BOB, "radar") == ([saved.item.id], [])
    flt_bob, flt_carol = read_filter(BOB), read_filter(CAROL)
    vec = (await stack.embedder.embed(["radar range equation"]))[0]
    assert await stack.backend.text.search(flt_bob, "radar", 5)
    assert await stack.backend.vectors.search(flt_bob, stack.embedder.model_id, vec, 5)
    assert not await stack.backend.text.search(flt_carol, "radar", 5)
    back = await stack.knowledge.move(
        ALICE, saved.item.id, Scope(ScopeLevel.PRIVATE, "alice"), Visibility.SCOPE, moved.revision
    )
    assert back.revision == 3
    assert not await stack.backend.text.search(flt_bob, "radar", 5)
    assert not await stack.backend.vectors.search(flt_bob, stack.embedder.model_id, vec, 5)


async def test_update_reindexes_content_tags_and_archiving(stack):
    saved = await stack.knowledge.remember(ALICE, req(body="old unicorn content"))
    await stack.knowledge.update(ALICE, saved.item.id, ItemPatch(body="fresh narwhal content"), 1)
    assert (await ids(stack, ALICE, "unicorn"))[0] == []
    assert (await ids(stack, ALICE, "narwhal"))[0] == [saved.item.id]
    tagged = await stack.knowledge.tag(ALICE, saved.item.id, ["Domain/Sonar"], [], 2)
    assert tagged.tags == {"domain/sonar"}
    assert (await ids(stack, ALICE, "sonar"))[0] == [saved.item.id]  # tags are searchable text
    assert (await ids(stack, ALICE, "narwhal", tags_all=frozenset({"domain/sonar"})))[0] == [
        saved.item.id
    ]
    assert (await ids(stack, ALICE, "narwhal", tags_all=frozenset({"other"})))[0] == []
    await stack.knowledge.update(ALICE, saved.item.id, ItemPatch(status=Status.ARCHIVED), 3)
    assert await ids(stack, ALICE, "narwhal") == ([], [])
    assert not await stack.backend.text.search(read_filter(ALICE), "narwhal", 5)


async def test_secrets_added_by_an_update_are_masked_too(stack):
    saved = await stack.knowledge.remember(ALICE, req())
    leak = "AKIAIOSFODNN7EXAMPLE"
    updated = await stack.knowledge.update(
        ALICE, saved.item.id, ItemPatch(body=f"key {leak} here"), 1
    )
    assert leak not in updated.body


async def test_kind_filter_and_limit(stack):
    for i in range(5):
        await stack.knowledge.remember(
            ALICE, req(title=f"Formula {i}", body="speed of light formula", kind=Kind.FORMULA)
        )
    await stack.knowledge.remember(ALICE, req(title="A note", body="speed of light note"))
    only_formulas = await ids(
        stack, ALICE, "speed of light", kinds=frozenset({Kind.FORMULA}), limit=3
    )
    assert len(only_formulas[0]) == 3
    everything = await stack.consult.consult(ALICE, ConsultRequest("speed of light", limit=10))
    assert len(everything.primary) == 6


async def test_snippet_comes_from_the_matching_chunk(stack):
    paragraphs = [
        " ".join(f"{name}{i}" for i in range(130)) for name in ("alpha", "bravo", "charlie")
    ]
    saved = await stack.knowledge.remember(
        ALICE, req(title="Long note", body="\n\n".join(paragraphs))
    )
    result = await stack.consult.consult(ALICE, ConsultRequest("charlie7 charlie8 charlie9"))
    assert result.primary[0].item_id == saved.item.id and "charlie" in result.primary[0].snippet


async def test_audit_trail_records_actions_without_content(stack):
    saved = await stack.knowledge.remember(ALICE, req(body="classified wombat content"))
    await stack.knowledge.update(ALICE, saved.item.id, ItemPatch(title="Renamed"), 1)
    await stack.knowledge.move(ALICE, saved.item.id, P1, Visibility.SCOPE, 2)
    events = [r.event for r in await stack.backend.audit.list("org1")]
    assert [e.action for e in events] == ["item.create", "item.update", "item.relocate"]
    assert all(e.actor_id == "alice" and e.target_id == saved.item.id for e in events)
    assert "wombat" not in repr(events)


class FlakyEmbedder:
    """Fails like an unreachable hosted provider until `up` is set."""

    def __init__(self, inner):
        self.inner, self.up = inner, False
        self.model_id, self.dim, self.min_similarity = (
            inner.model_id,
            inner.dim,
            inner.min_similarity,
        )

    async def embed(self, texts):
        if not self.up:
            raise EmbeddingUnavailable("provider down")
        return await self.inner.embed(texts)

    async def embed_query(self, text):
        if not self.up:
            raise EmbeddingUnavailable("provider down")
        return await self.inner.embed_query(text)


async def test_embedding_outage_never_loses_items_and_recovers_via_job(backend):
    flaky = FlakyEmbedder(HashingEmbedder(128))
    stack = build_stack(backend, flaky)
    saved = await stack.knowledge.remember(ALICE, req(body="submarine cable repair"))
    assert saved.created and not saved.indexed
    assert (await stack.backend.items.get(ALICE, saved.item.id)).item.id == saved.item.id  # stored
    job = await backend.jobs.claim("w", frozenset({INDEX_JOB}), 30)
    assert job and job.payload == {"item_id": saved.item.id}
    result = await stack.consult.consult(ALICE, ConsultRequest("submarine cable"))
    assert any(
        "unavailable" in n for n in result.notes
    )  # degrades to keyword search, tells the caller
    assert [m.item_id for m in result.primary] == [
        saved.item.id
    ]  # keyword index was already written
    with pytest.raises(EmbeddingUnavailable):
        await stack.indexing.handle_job(job)  # still down: the runner would retry
    flaky.up = True
    await stack.indexing.handle_job(job)
    assert (await ids(stack, ALICE, "submarine cable"))[0] == [saved.item.id]
    assert await backend.jobs.complete(job.id, "w")


async def test_indexing_a_deleted_or_foreign_item_is_safe(stack):
    assert await stack.indexing.index_current("org1", "does-not-exist")
    saved = await stack.knowledge.remember(ALICE, req())
    assert await stack.indexing.index_current(
        "org2", saved.item.id
    )  # wrong tenant: treated as absent
    assert (await ids(stack, ALICE, "radar"))[0] == [saved.item.id]


async def test_item_changing_while_it_is_indexed_ends_up_consistent(backend):
    inner = HashingEmbedder(128)

    class Racer:
        model_id, dim, min_similarity = inner.model_id, inner.dim, inner.min_similarity
        fired = False

        async def embed(self, texts):
            if not self.fired:
                self.fired = (
                    True  # a concurrent writer edits the item during the first indexing pass
                )
                await racer_stack.knowledge._items.update(
                    ALICE, item_id, ItemPatch(body="second version pelican"), 1
                )
            return await inner.embed(texts)

        async def embed_query(self, text):
            return await inner.embed_query(text)

    racer_stack = build_stack(backend, Racer())
    base = await racer_stack.knowledge._items.create(
        ALICE,
        __import__("tests.conformance.factory", fromlist=["x"]).make_item(
            backend.clock, body="first version walrus"
        ),
    )
    item_id = base.item.id
    assert await racer_stack.indexing.index_current("org1", item_id)
    assert (await ids(racer_stack, ALICE, "pelican"))[0] == [item_id]
    assert (await ids(racer_stack, ALICE, "walrus"))[0] == []


async def test_get_and_missing(stack):
    saved = await stack.knowledge.remember(ALICE, req())
    assert (await stack.knowledge.get(ALICE, saved.item.id)).item == saved.item
    with pytest.raises(NotFound):
        await stack.knowledge.get(BOB, saved.item.id)


@pytest.mark.parametrize(
    ("scope", "vis"),
    [
        (Scope(ScopeLevel.TEAM, "t1"), Visibility.SCOPE),
        (Scope(ScopeLevel.ORG, "org1"), Visibility.SCOPE),
        (P1, Visibility.ORG),
    ],
)
async def test_members_cannot_promote_wide_but_approvers_can(stack, scope, vis):
    mine = await stack.knowledge.remember(ALICE, req())
    with pytest.raises(PermissionDenied, match="approval"):
        await stack.knowledge.move(ALICE, mine.item.id, scope, vis, 1)
    unchanged = await stack.knowledge.get(ALICE, mine.item.id)
    assert unchanged.item.scope.level is ScopeLevel.PRIVATE and unchanged.item.revision == 1
    # An approver (admin) can publish an item they own to a scope they belong to.
    theirs = await stack.knowledge.remember(ROOT, req(title="Approver note"))
    org_scope = Scope(ScopeLevel.ORG, "org1")
    published = await stack.knowledge.move(ROOT, theirs.item.id, org_scope, Visibility.ORG, 1)
    assert published.visibility is Visibility.ORG
    # Org-level knowledge belongs to every member: primary for Carol, not "external".
    assert (await ids(stack, CAROL, "approver note")) == ([theirs.item.id], [])


async def test_superseded_items_are_hidden_by_default_and_rank_below_current_ones(stack):
    old = await stack.knowledge.remember(
        ALICE, req(title="Pulse timing v1", body="pulse timing guidance")
    )
    new = await stack.knowledge.remember(
        ALICE, req(title="Pulse timing v2", body="pulse timing guidance")
    )
    draft = await stack.knowledge.remember(
        ALICE, req(title="Pulse timing draft", body="pulse timing guidance")
    )
    await stack.knowledge.update(ALICE, old.item.id, ItemPatch(status=Status.SUPERSEDED), 1)
    await stack.knowledge.update(ALICE, draft.item.id, ItemPatch(status=Status.DRAFT), 1)
    default = (await ids(stack, ALICE, "pulse timing"))[0]
    assert old.item.id not in default and {new.item.id, draft.item.id} <= set(default)
    assert default.index(new.item.id) < default.index(draft.item.id)  # active outranks draft
    everything = frozenset(Status) - {Status.ARCHIVED}
    with_old = (await ids(stack, ALICE, "pulse timing", statuses=everything))[0]
    assert with_old[-1] == old.item.id  # superseded knowledge is last, not lost
