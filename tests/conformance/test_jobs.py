import asyncio

from hive_mcp.ports.jobs import JobState

KINDS = frozenset({"ingest", "embed"})


async def test_enqueue_claim_complete(backend):
    job = await backend.jobs.enqueue("org1", "ingest", {"path": "a.md"})
    claimed = await backend.jobs.claim("w1", KINDS, 30)
    assert claimed and claimed.id == job.id and claimed.attempts == 1
    assert claimed.state is JobState.RUNNING and claimed.payload == {"path": "a.md"}
    assert await backend.jobs.claim("w2", KINDS, 30) is None  # leased
    assert await backend.jobs.complete(job.id, "w1")
    assert (await backend.jobs.get(job.id)).state is JobState.DONE


async def test_kinds_and_order_and_delay(backend):
    other = await backend.jobs.enqueue("org1", "reindex", {})
    first = await backend.jobs.enqueue("org1", "ingest", {})
    backend.clock.advance(seconds=1)
    second = await backend.jobs.enqueue("org1", "embed", {})
    delayed = await backend.jobs.enqueue("org1", "ingest", {}, delay_seconds=60)
    assert (await backend.jobs.claim("w", KINDS, 30)).id == first.id
    assert (await backend.jobs.claim("w", KINDS, 30)).id == second.id
    assert await backend.jobs.complete(first.id, "w") and await backend.jobs.complete(
        second.id, "w"
    )
    assert (
        await backend.jobs.claim("w", KINDS, 30) is None
    )  # delayed one not due, reindex not requested
    backend.clock.advance(seconds=61)
    assert (await backend.jobs.claim("w", KINDS, 30)).id == delayed.id
    assert (await backend.jobs.get(other.id)).state is JobState.QUEUED


async def test_dedupe_blocks_only_live_jobs(backend):
    a = await backend.jobs.enqueue("org1", "ingest", {}, dedupe_key="repo-x")
    again = await backend.jobs.enqueue("org1", "ingest", {}, dedupe_key="repo-x")
    other_org = await backend.jobs.enqueue("org2", "ingest", {}, dedupe_key="repo-x")
    assert again.id == a.id and other_org.id != a.id
    claimed = await backend.jobs.claim("w", KINDS, 30)
    assert (
        await backend.jobs.enqueue("org1", "ingest", {}, dedupe_key="repo-x")
    ).id == a.id  # running
    await backend.jobs.complete(claimed.id, "w")
    assert (await backend.jobs.enqueue("org1", "ingest", {}, dedupe_key="repo-x")).id != a.id


async def test_expired_lease_is_reclaimed_and_old_worker_is_fenced(backend):
    job = await backend.jobs.enqueue("org1", "ingest", {})
    await backend.jobs.claim("crashed", KINDS, 30)
    backend.clock.advance(seconds=31)
    reclaimed = await backend.jobs.claim("w2", KINDS, 30)
    assert reclaimed and reclaimed.id == job.id and reclaimed.attempts == 2
    assert not await backend.jobs.complete(job.id, "crashed")  # zombie must not finish it
    assert not await backend.jobs.fail(job.id, "crashed", "boom", 0)
    assert not await backend.jobs.heartbeat(job.id, "crashed", 30)
    assert await backend.jobs.complete(job.id, "w2")


async def test_heartbeat_extends_the_lease(backend):
    job = await backend.jobs.enqueue("org1", "ingest", {})
    await backend.jobs.claim("w1", KINDS, 30)
    backend.clock.advance(seconds=20)
    assert await backend.jobs.heartbeat(job.id, "w1", 30)
    backend.clock.advance(seconds=20)  # 40s since claim, but lease was renewed
    assert await backend.jobs.claim("w2", KINDS, 30) is None


async def test_fail_retries_then_goes_dead(backend):
    job = await backend.jobs.enqueue("org1", "ingest", {}, max_attempts=2)
    await backend.jobs.claim("w", KINDS, 30)
    assert await backend.jobs.fail(job.id, "w", "first", retry_in_seconds=10)
    assert (await backend.jobs.get(job.id)).state is JobState.QUEUED
    assert await backend.jobs.claim("w", KINDS, 30) is None  # backing off
    backend.clock.advance(seconds=11)
    await backend.jobs.claim("w", KINDS, 30)
    assert await backend.jobs.fail(job.id, "w", "second", retry_in_seconds=10)
    dead = await backend.jobs.get(job.id)
    assert dead.state is JobState.DEAD and dead.last_error == "second"


async def test_crash_loop_ends_dead_instead_of_retrying_forever(backend):
    job = await backend.jobs.enqueue("org1", "ingest", {}, max_attempts=2)
    for worker in ("w1", "w2"):
        assert await backend.jobs.claim(worker, KINDS, 10)
        backend.clock.advance(seconds=11)  # worker dies without reporting
    assert await backend.jobs.claim("w3", KINDS, 10) is None
    assert (await backend.jobs.get(job.id)).state is JobState.DEAD


async def test_concurrent_dedupe_enqueue_creates_one_job(backend):
    # A single burst can slip through a missing lock by luck, so hammer many independent keys.
    for round_no in range(40):
        key = f"same-{round_no}"
        jobs = await asyncio.gather(
            *(backend.jobs.enqueue("org1", "ingest", {}, dedupe_key=key) for _ in range(8))
        )
        assert len({j.id for j in jobs}) == 1, f"duplicate jobs for {key}"
