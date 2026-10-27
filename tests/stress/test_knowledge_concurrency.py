"""Many callers at once through the service layer: no lost updates, conflicts are reported,
idempotency keys never duplicate, and organisations never see each other."""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from hive_mcp.domain.errors import Conflict, IdempotencyConflict
from hive_mcp.domain.item import ItemPatch, ItemQuery, Status
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.services.models import ConsultRequest, RememberRequest
from tests.stress.support import SCALE, gather_bounded, rng

pytestmark = pytest.mark.stress

P1 = Scope(ScopeLevel.PROJECT, "p1")
ORGS = ("acme", "globex")
# the same user and project ids in both organisations: only the org id tells them apart
USERS = {
    org: [Principal(f"u{i}", org, Role.EDITOR, projects=frozenset({"p1"})) for i in range(4)]
    for org in ORGS
}


async def all_visible(stack, who: Principal) -> list:
    out, after = [], None
    while True:
        page = await stack.knowledge.browse(
            who, ItemQuery(limit=100, after=after, statuses=frozenset(Status))
        )
        out += [v.item for v in page.items]
        if page.next_cursor is None:
            return out
        from hive_mcp.domain.item import Cursor

        after = Cursor.decode(page.next_cursor)


async def test_concurrent_updates_from_one_revision_exactly_one_wins(stack) -> None:
    who = USERS["acme"][0]
    note = (
        await stack.knowledge.remember(
            who, RememberRequest("Shared runbook", "version one", scope=P1)
        )
    ).item
    racers = 12 * SCALE
    users = USERS["acme"]
    results = await gather_bounded(
        [
            (
                lambda i=i: stack.knowledge.update(
                    users[i % 4], note.id, ItemPatch(body=f"edit number {i}"), note.revision
                )
            )
            for i in range(racers)
        ]
    )
    wins = [r for r in results if not isinstance(r, BaseException)]
    losses = [r for r in results if isinstance(r, BaseException)]
    assert len(wins) == 1, "exactly one writer may use revision 1"
    assert all(isinstance(e, Conflict) for e in losses), Counter(type(e).__name__ for e in losses)
    assert len(losses) == racers - 1
    final = (await stack.knowledge.get(who, note.id)).item
    assert final.revision == 2 and final.body == wins[0].body  # the winner's text, not a mix


async def test_read_modify_write_loops_lose_no_update_and_revisions_increase(stack) -> None:
    users = USERS["acme"]
    note = (
        await stack.knowledge.remember(users[0], RememberRequest("Counter", "start", scope=P1))
    ).item
    per_user = 5 * min(SCALE, 2)  # stays inside the 50 stored versions of the history
    conflicts: list[int] = []

    async def worker(who: Principal, n: int) -> list[int]:
        got = []
        for k in range(per_user):
            while True:
                current = (await stack.knowledge.get(who, note.id)).item
                try:
                    done = await stack.knowledge.update(
                        who, note.id, ItemPatch(add_tags=frozenset({f"w{n}/op{k}"})),
                        current.revision,
                    )  # fmt: skip
                except Conflict:
                    conflicts.append(1)
                    continue
                got.append(done.revision)
                break
        return got

    per_worker = await asyncio.gather(*(worker(u, n) for n, u in enumerate(users)))
    total = per_user * len(users)
    revisions = sorted(r for got in per_worker for r in got)
    assert revisions == list(range(2, total + 2)), "every success owns a distinct revision"
    for got in per_worker:
        assert got == sorted(got) and len(set(got)) == len(got)  # strictly increasing per caller
    final = (await stack.knowledge.get(users[0], note.id)).item
    assert final.revision == total + 1
    expected = {f"w{n}/op{k}" for n in range(len(users)) for k in range(per_user)}
    assert expected <= final.tags, "tag additions of concurrent editors were lost"
    versions = await stack.knowledge.history(users[0], note.id, limit=50)
    seen = [v.revision for v in versions]
    assert seen == sorted(seen, reverse=True) and len(set(seen)) == len(seen)
    assert seen[0] == total + 1 and seen == list(range(total + 1, total + 1 - len(seen), -1))


async def test_a_mixed_storm_across_organisations_stays_consistent(stack) -> None:
    """Writes, edits and searches of both tenants interleaved; nothing leaks, nothing is lost."""
    gen = rng(1)
    per_user = 10 * SCALE
    made: dict[str, tuple[str, str]] = {}  # item id -> (org, author)
    tokens: dict[str, str] = {}  # unique word -> org

    async def write(who: Principal, i: int) -> None:
        word = f"zq{who.org_id[:2]}{who.id}x{i}"
        r = await stack.knowledge.remember(
            who,
            RememberRequest(f"Note {word}", f"{word} pelican shared words {i}", scope=P1),
        )
        made[r.item.id] = (who.org_id, who.id)
        tokens[word] = who.org_id

    jobs = [
        (lambda u=u, i=i: write(u, i)) for org in ORGS for u in USERS[org] for i in range(per_user)
    ]
    gen.shuffle(jobs)
    assert not [r for r in await gather_bounded(jobs, 32) if isinstance(r, BaseException)]
    assert len(made) == per_user * 8

    # now edit half of them while everybody searches
    edits, searches = [], []
    for item_id, (org, _author) in list(made.items())[::2]:
        who = USERS[org][0]
        edits.append(lambda who=who, item_id=item_id: _edit_with_retry(stack, who, item_id))
    for u in [u for org in ORGS for u in USERS[org]]:
        searches.append(
            lambda u=u: stack.consult.consult(u, ConsultRequest("pelican shared", limit=50))
        )
    outcomes = await gather_bounded(edits + searches, 32)
    assert not [r for r in outcomes if isinstance(r, BaseException)]
    for res in outcomes[len(edits) :]:
        for m in (*res.primary, *res.referenced):
            assert m.item_id in made
    consult_by_org = {}
    for org in ORGS:
        who = USERS[org][1]
        visible = await all_visible(stack, who)
        assert {i.id for i in visible} == {i for i, (o, _) in made.items() if o == org}
        assert all(i.org_id == org for i in visible)
        consult_by_org[org] = who
    # a token written in one tenant is invisible in the other, and found in its own
    for word, org in list(tokens.items())[:: max(1, len(tokens) // 25)]:
        other = "globex" if org == "acme" else "acme"
        mine = await stack.consult.consult(consult_by_org[org], ConsultRequest(word, limit=50))
        theirs = await stack.consult.consult(consult_by_org[other], ConsultRequest(word))
        assert f"Note {word}" in [m.title for m in mine.primary]
        foreign = [m.item_id for m in (*theirs.primary, *theirs.referenced)]
        # fuzzy vector matches may return the other tenant's own notes, never this tenant's
        assert all(made[i][0] == other for i in foreign), (word, org, foreign)
        assert not [i for i in foreign if made[i][0] == org]


async def _edit_with_retry(stack, who: Principal, item_id: str) -> None:
    for _ in range(50):
        current = (await stack.knowledge.get(who, item_id)).item
        try:
            await stack.knowledge.update(
                who, item_id, ItemPatch(add_tags=frozenset({"edited"})), current.revision
            )
            return
        except Conflict:
            continue
    raise AssertionError("could not edit")


async def test_idempotency_key_never_creates_a_duplicate_under_races(stack) -> None:
    keys, racers = 6 * SCALE, 8
    ann, bob = USERS["acme"][0], USERS["acme"][1]
    globex = USERS["globex"][0]  # same user id as ann, other organisation

    def req(k: int, payload: str = "same payload") -> RememberRequest:
        return RememberRequest(f"Idem {k}", payload, scope=P1, idempotency_key=f"key-{k}")

    jobs = [
        (lambda k=k, who=who: stack.knowledge.remember(who, req(k)))
        for k in range(keys)
        for who in (ann, bob, globex)
        for _ in range(racers)
    ]
    results = await gather_bounded(jobs, 48)
    assert not [r for r in results if isinstance(r, BaseException)]
    groups: dict[tuple[str, str, str], list] = {}
    for job_i, r in enumerate(results):
        k, rest = divmod(job_i, 3 * racers)
        who = (ann, bob, globex)[rest // racers]
        groups.setdefault((who.org_id, who.id, f"key-{k}"), []).append(r)
    assert len(groups) == keys * 3
    for group in groups.values():
        assert sum(r.created for r in group) == 1, "exactly one create per (org, author, key)"
        assert len({r.item.id for r in group}) == 1, "every replay returns the first item"
    ids = {r.item.id for g in groups.values() for r in g}
    assert len(ids) == keys * 3  # authors and organisations never share a key's namespace
    for who, count in ((ann, keys * 2), (globex, keys)):
        assert len(await all_visible(stack, who)) == count  # ann and bob share p1


async def test_one_key_with_different_payloads_admits_exactly_one(stack) -> None:
    who = USERS["acme"][2]
    jobs = [
        (
            lambda i=i: stack.knowledge.remember(
                who,
                RememberRequest("Same title", f"payload {i}", idempotency_key="shared-key"),
            )
        )
        for i in range(10 * SCALE)
    ]
    results = await gather_bounded(jobs, 32)
    ok = [r for r in results if not isinstance(r, BaseException)]
    bad = [r for r in results if isinstance(r, BaseException)]
    assert len(ok) == 1 and ok[0].created
    assert bad and all(isinstance(e, IdempotencyConflict) for e in bad)
    assert len(await all_visible(stack, who)) == 1


async def test_org_isolation_for_direct_gets_and_updates(stack) -> None:
    ann = USERS["acme"][0]
    spy = USERS["globex"][0]
    items = [
        (
            await stack.knowledge.remember(
                ann, RememberRequest(f"Secret {i}", f"quokka {i}", scope=P1)
            )
        ).item
        for i in range(10)
    ]
    from hive_mcp.domain.errors import NotFound

    async def probe(item) -> BaseException | None:
        try:
            await stack.knowledge.get(spy, item.id)
        except NotFound:
            pass
        else:
            return AssertionError("leak")
        try:
            await stack.knowledge.update(spy, item.id, ItemPatch(body="pwned"), item.revision)
        except NotFound:
            return None
        return AssertionError("write across organisations")

    outcomes = await gather_bounded([(lambda i=i: probe(i)) for i in items * (3 * SCALE)], 32)
    assert outcomes == [None] * len(outcomes)
    for item in items:
        assert (await stack.knowledge.get(ann, item.id)).item.revision == 1
