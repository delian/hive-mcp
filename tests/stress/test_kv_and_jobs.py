"""The key-value store and the job queue with many concurrent callers: single winners, no job
handled twice, retries with backoff, expired leases reclaimed and the old holder fenced out."""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from hive_mcp.ports.jobs import JobState
from hive_mcp.services.runner import BASE_BACKOFF_SECONDS, JobRunner, backoff_seconds
from tests.stress.support import SCALE, gather_bounded

pytestmark = pytest.mark.stress
NS = "stress"
KINDS = frozenset({"work", "other"})


# ------------------------------------------------------------------------------------------ KV
async def test_put_if_absent_has_exactly_one_winner_per_key(backend) -> None:
    keys, callers = 40 * SCALE, 16
    jobs = [
        (lambda k=k, c=c: backend.kv.put_if_absent(NS, f"k{k}", {"by": c}))
        for k in range(keys)
        for c in range(callers)
    ]
    results = await gather_bounded(jobs, 48)
    assert not [r for r in results if isinstance(r, BaseException)]
    for k in range(keys):
        group = results[k * callers : (k + 1) * callers]
        assert sum(group) == 1, f"key {k}: {sum(group)} winners"
        stored = await backend.kv.get(NS, f"k{k}")
        assert stored == {"by": group.index(True)}, "the stored value is the winner's"


async def test_take_has_exactly_one_winner_per_key(backend) -> None:
    keys, callers = 40 * SCALE, 16
    for k in range(keys):
        await backend.kv.put(NS, f"code{k}", {"n": k})
    jobs = [
        (lambda k=k: backend.kv.take(NS, f"code{k}")) for k in range(keys) for _ in range(callers)
    ]
    results = await gather_bounded(jobs, 48)
    for k in range(keys):
        got = [r for r in results[k * callers : (k + 1) * callers] if r is not None]
        assert got == [{"n": k}], f"key {k} replayed or lost: {got}"
        assert await backend.kv.get(NS, f"code{k}") is None


async def test_put_if_absent_racing_take_never_loses_or_duplicates(backend) -> None:
    """Each round: one record, takers and creators at once. Creators win only after a take."""
    for round_no in range(15 * SCALE):
        key = f"flip{round_no}"
        await backend.kv.put(NS, key, {"gen": 0})
        jobs = [(lambda key=key: backend.kv.take(NS, key))] * 4 + [
            (lambda i=i, key=key: backend.kv.put_if_absent(NS, key, {"gen": i}))
            for i in range(1, 5)
        ]
        results = await gather_bounded(jobs, 16)
        assert not [r for r in results if isinstance(r, BaseException)]
        takes = [r for r in results[:4] if r is not None]
        creates = [r for r in results[4:] if r is True]
        # successes alternate take, create, take, ...: one record is never taken or made twice
        assert len(takes) - len(creates) in (0, 1), (len(takes), len(creates))
        assert (await backend.kv.get(NS, key) is not None) == (len(takes) == len(creates))


async def test_ttl_expiry_under_a_fixed_clock_with_races(backend) -> None:
    n = 30 * SCALE
    for i in range(n):
        await backend.kv.put(NS, f"short{i}", {"i": i}, ttl_seconds=10)
        await backend.kv.put(NS, f"long{i}", {"i": i}, ttl_seconds=1000)
        await backend.kv.put(NS, f"stale{i}", {"i": i}, ttl_seconds=10)  # nobody touches these
        await backend.kv.put(NS, f"forever{i}", {"i": i})
    backend.clock.advance(seconds=9)
    assert all([await backend.kv.get(NS, f"short{i}") for i in range(n)])
    backend.clock.advance(seconds=2)  # now 11 s: the short ones are gone
    results = await gather_bounded(
        [(lambda i=i: backend.kv.get(NS, f"short{i}")) for i in range(n)]
        + [(lambda i=i: backend.kv.take(NS, f"short{i}")) for i in range(n)]
        + [(lambda i=i: backend.kv.get(NS, f"long{i}")) for i in range(n)]
    )
    assert results[: 2 * n] == [None] * (2 * n), "an expired record is still readable or takeable"
    assert results[2 * n :] == [{"i": i} for i in range(n)]
    # the expired slot is free again: exactly one of many creators wins, with a fresh TTL
    wins = await gather_bounded(
        [(lambda i=i: backend.kv.put_if_absent(NS, "short0", {"again": i}, ttl_seconds=5))
         for i in range(20)]
    )  # fmt: skip
    assert sum(w is True for w in wins) == 1
    assert await backend.kv.purge_expired() >= n  # untouched expired rows are removed physically
    assert await backend.kv.purge_expired() == 0
    backend.clock.advance(seconds=6)
    assert await backend.kv.get(NS, "short0") is None
    assert all([await backend.kv.get(NS, f"forever{i}") == {"i": i} for i in range(n)])
    assert all([await backend.kv.get(NS, f"long{i}") == {"i": i} for i in range(n)])


async def test_namespaces_stay_separate_under_load(backend) -> None:
    await gather_bounded(
        [
            (lambda ns=ns, i=i: backend.kv.put(ns, f"k{i}", {"ns": ns}))
            for ns in "abc"
            for i in range(30 * SCALE)
        ]
    )
    for ns in "abc":
        assert all([await backend.kv.get(ns, f"k{i}") == {"ns": ns} for i in range(30 * SCALE)])
    taken = await gather_bounded(
        [(lambda i=i: backend.kv.take("a", f"k{i}")) for i in range(30 * SCALE)]
    )
    assert all(t == {"ns": "a"} for t in taken)
    assert all([await backend.kv.get("b", f"k{i}") == {"ns": "b"} for i in range(30 * SCALE)])


# ----------------------------------------------------------------------------------------- jobs
class Ledger:
    """Counts handler invocations per job and checks nobody holds a job twice at the same time."""

    def __init__(self) -> None:
        self.runs: Counter[str] = Counter()
        self.active: set[str] = set()
        self.overlap: list[str] = []

    async def ok(self, job) -> None:
        if job.id in self.active:
            self.overlap.append(job.id)
        self.active.add(job.id)
        self.runs[job.id] += 1
        await asyncio.sleep(0)  # let other workers claim while this one "works"
        self.active.discard(job.id)


async def run_workers(backend, handlers, workers: int, lease: float = 60) -> int:
    runners = [
        JobRunner(backend.jobs, handlers, worker_id=f"w{i}", lease_seconds=lease, poll_seconds=0)
        for i in range(workers)
    ]

    async def loop(runner: JobRunner) -> int:
        done = 0
        idle = 0
        while idle < 3:
            if await runner.run_once():
                done, idle = done + 1, 0
            else:
                idle += 1
                await asyncio.sleep(0)
        return done

    return sum(await asyncio.gather(*(loop(r) for r in runners)))


async def test_many_workers_handle_every_job_exactly_once(backend) -> None:
    total = 120 * SCALE
    ledger = Ledger()
    enqueued = await gather_bounded(
        [
            (
                lambda i=i: backend.jobs.enqueue(
                    f"org{i % 3}", "work" if i % 4 else "other", {"i": str(i)}
                )
            )
            for i in range(total)
        ],
        32,
    )
    ids = {j.id for j in enqueued}
    assert len(ids) == total
    handled = await run_workers(backend, {"work": ledger.ok, "other": ledger.ok}, workers=10)
    assert handled == total and set(ledger.runs) == ids
    assert all(count == 1 for count in ledger.runs.values()), "a job ran twice"
    assert not ledger.overlap
    assert all([(await backend.jobs.get(i)).state is JobState.DONE for i in ids])
    assert await backend.jobs.stats() == []


async def test_a_worker_only_gets_the_kinds_it_asks_for(backend) -> None:
    for i in range(20):
        await backend.jobs.enqueue("org1", "work", {"i": str(i)})
        await backend.jobs.enqueue("org1", "other", {"i": str(i)})
    seen: Counter[str] = Counter()

    async def only_work(job) -> None:
        seen[job.kind] += 1

    assert await run_workers(backend, {"work": only_work}, workers=4) == 20
    assert seen == {"work": 20}
    assert {s.kind: s.count for s in await backend.jobs.stats()} == {"other": 20}


async def test_failures_retry_with_backoff_then_succeed_or_die(backend) -> None:
    flaky = [
        await backend.jobs.enqueue("org1", "work", {"mode": "flaky", "n": str(i)})
        for i in range(20 * SCALE)
    ]
    doomed = [
        await backend.jobs.enqueue("org1", "work", {"mode": "doomed"}, max_attempts=3)
        for _ in range(5)
    ]
    attempts: Counter[str] = Counter()

    async def handler(job) -> None:
        attempts[job.id] += 1
        if job.payload["mode"] == "doomed" or job.attempts < 3:
            raise RuntimeError(f"boom {job.attempts}")

    # nothing is due again until the clock moves past the backoff
    first = await run_workers(backend, {"work": handler}, workers=6)
    assert first == len(flaky) + len(doomed)
    assert await run_workers(backend, {"work": handler}, workers=6) == 0, (
        "retried before its backoff"
    )
    stats = {s.state: s.count for s in await backend.jobs.stats() if s.kind == "work"}
    assert stats == {JobState.QUEUED: len(flaky) + len(doomed)}
    backend.clock.advance(seconds=backoff_seconds(1) - 1)
    assert await run_workers(backend, {"work": handler}, workers=6) == 0  # just short of it
    backend.clock.advance(seconds=2)
    assert await run_workers(backend, {"work": handler}, workers=6) == len(flaky) + len(doomed)
    backend.clock.advance(seconds=backoff_seconds(2) + 1)  # the second backoff is twice as long
    assert backoff_seconds(2) == BASE_BACKOFF_SECONDS * 2
    await run_workers(backend, {"work": handler}, workers=6)
    for job in flaky:
        done = await backend.jobs.get(job.id)
        assert done.state is JobState.DONE and done.attempts == 3 == attempts[job.id]
    for job in doomed:
        dead = await backend.jobs.get(job.id)
        assert dead.state is JobState.DEAD and dead.attempts == 3
        assert dead.last_error == "boom 3"
    backend.clock.advance(seconds=10_000)
    assert await run_workers(backend, {"work": handler}, workers=6) == 0  # DEAD stays dead


async def test_crashed_workers_leases_expire_and_the_old_holder_is_fenced(backend) -> None:
    total = 30 * SCALE
    for i in range(total):
        await backend.jobs.enqueue("org1", "work", {"i": str(i)})
    claimed = []
    for i in range(total):  # a fleet of workers each takes a job and then dies
        job = await backend.jobs.claim(f"dead{i}", KINDS, 30)
        assert job and job.attempts == 1
        claimed.append((job, f"dead{i}"))
    assert await backend.jobs.claim("early", KINDS, 30) is None, "a leased job was handed out"
    backend.clock.advance(seconds=31)
    ledger = Ledger()
    assert await run_workers(backend, {"work": ledger.ok}, workers=8) == total
    assert all(c == 1 for c in ledger.runs.values()) and len(ledger.runs) == total
    for job, worker in claimed:
        now = await backend.jobs.get(job.id)
        assert now.state is JobState.DONE and now.attempts == 2
        # the zombie wakes up and tries to report: nothing it does is accepted
        results = await asyncio.gather(
            backend.jobs.complete(job.id, worker),
            backend.jobs.fail(job.id, worker, "late", 0),
            backend.jobs.heartbeat(job.id, worker, 30),
        )
        assert results == [False, False, False]
        assert (await backend.jobs.get(job.id)).state is JobState.DONE


async def test_heartbeats_keep_a_slow_job_away_from_other_workers(backend) -> None:
    job = await backend.jobs.enqueue("org1", "work", {})
    held = await backend.jobs.claim("slow", KINDS, 30)
    assert held
    for _ in range(10 * SCALE):
        backend.clock.advance(seconds=20)
        assert await backend.jobs.heartbeat(job.id, "slow", 30)
        results = await gather_bounded([(lambda: backend.jobs.claim("thief", KINDS, 30))] * 8)
        assert results == [None] * 8
    backend.clock.advance(seconds=31)  # stops beating: now it can be taken, by exactly one
    results = await gather_bounded(
        [(lambda i=i: backend.jobs.claim(f"t{i}", KINDS, 30)) for i in range(8)]
    )
    taken = [r for r in results if r is not None]
    assert len(taken) == 1 and taken[0].id == job.id and taken[0].attempts == 2


async def test_crash_loop_ends_dead_even_with_many_workers_racing(backend) -> None:
    jobs = [
        await backend.jobs.enqueue("org1", "work", {}, max_attempts=3) for _ in range(10 * SCALE)
    ]
    for _round in range(3):
        got = await gather_bounded(
            [(lambda i=i: backend.jobs.claim(f"w{i}", KINDS, 10)) for i in range(len(jobs) * 2)], 32
        )
        assert len([g for g in got if g]) == len(jobs)
        backend.clock.advance(seconds=11)  # every worker dies
    assert await backend.jobs.claim("w", KINDS, 10) is None
    assert all([(await backend.jobs.get(j.id)).state is JobState.DEAD for j in jobs])


async def test_dedupe_keys_race_to_one_live_job_each(backend) -> None:
    keys = 20 * SCALE
    results = await gather_bounded(
        [
            (lambda k=k: backend.jobs.enqueue("org1", "work", {}, dedupe_key=f"k{k}"))
            for k in range(keys)
            for _ in range(8)
        ],
        48,
    )
    assert not [r for r in results if isinstance(r, BaseException)]
    for k in range(keys):
        assert len({j.id for j in results[k * 8 : (k + 1) * 8]}) == 1
    assert sum(s.count for s in await backend.jobs.stats()) == keys
