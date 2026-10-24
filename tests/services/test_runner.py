import asyncio

import pytest

from hive_mcp.domain.errors import StorageBusy
from hive_mcp.ports.jobs import JobState
from hive_mcp.services.runner import (
    BASE_BACKOFF_SECONDS,
    MAX_BACKOFF_SECONDS,
    JobRunner,
    backoff_seconds,
)


def test_backoff_doubles_and_is_capped():
    assert (
        backoff_seconds(1) == BASE_BACKOFF_SECONDS
        and backoff_seconds(2) == 2 * BASE_BACKOFF_SECONDS
    )
    assert backoff_seconds(0) == BASE_BACKOFF_SECONDS and backoff_seconds(50) == MAX_BACKOFF_SECONDS


async def test_success_completes_and_unknown_kinds_are_left_alone(backend):
    done = []

    async def handler(job):
        done.append(job.payload["n"])

    mine = await backend.jobs.enqueue("org1", "work", {"n": "1"})
    other = await backend.jobs.enqueue("org1", "other-kind", {"n": "2"})
    runner = JobRunner(backend.jobs, {"work": handler}, worker_id="w")
    assert await runner.run_once() and not await runner.run_once()
    assert done == ["1"]
    assert (await backend.jobs.get(mine.id)).state is JobState.DONE
    assert (await backend.jobs.get(other.id)).state is JobState.QUEUED


async def test_failure_requeues_with_backoff_then_dies(backend):
    async def broken(job):
        raise RuntimeError("boom " + "x" * 1000)

    job = await backend.jobs.enqueue("org1", "work", {}, max_attempts=2)
    runner = JobRunner(backend.jobs, {"work": broken}, worker_id="w")
    assert await runner.run_once()
    queued = await backend.jobs.get(job.id)
    assert (
        queued.state is JobState.QUEUED
        and queued.last_error.startswith("boom")
        and len(queued.last_error) <= 500
    )
    assert not await runner.run_once()  # backing off
    backend.clock.advance(seconds=BASE_BACKOFF_SECONDS + 1)
    assert await runner.run_once()
    assert (await backend.jobs.get(job.id)).state is JobState.DEAD


async def test_cancellation_leaves_the_job_leased_not_failed(backend):
    started = asyncio.Event()

    async def slow(job):
        started.set()
        await asyncio.sleep(60)

    job = await backend.jobs.enqueue("org1", "work", {})
    runner = JobRunner(backend.jobs, {"work": slow}, worker_id="w", lease_seconds=30)
    task = asyncio.create_task(runner.run_once())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    leased = await backend.jobs.get(job.id)
    assert leased.state is JobState.RUNNING and leased.last_error is None
    backend.clock.advance(seconds=31)  # the lease expires and another worker retries
    assert (await backend.jobs.claim("w2", frozenset({"work"}), 30)).id == job.id


async def test_run_forever_survives_storage_hiccups():
    calls = []

    class Flaky:
        async def claim(self, *args):
            calls.append(1)
            if len(calls) < 3:
                raise StorageBusy("db down")
            raise asyncio.CancelledError

    runner = JobRunner(Flaky(), {"work": None}, worker_id="w", poll_seconds=0.01)
    with pytest.raises(asyncio.CancelledError):
        await runner.run_forever()
    assert len(calls) == 3
