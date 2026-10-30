import asyncio

import pytest

from hive_mcp.domain.errors import ValidationError
from hive_mcp.ports.maintenance import ItemFilter
from hive_mcp.services import bulk_files
from hive_mcp.services.bulk_ingest import INGEST_JOB, BulkIngest
from hive_mcp.services.runner import JobRunner


def bulk(stack, **kw):
    kw.setdefault("step_files", 2)
    return BulkIngest(stack.ingest, stack.backend.jobs, stack.backend.kv, stack.backend.clock, **kw)


def runner(stack, svc, name="w1"):
    return JobRunner(stack.backend.jobs, {INGEST_JOB: svc.handle_step}, worker_id=name)


async def drain(run, limit=500):
    for _ in range(limit):
        if not await run.run_once():
            return
    raise AssertionError("did not settle")


async def stored(stack):
    flt = ItemFilter(org_id="org1")
    return await stack.backend.maintenance.find_system(flt, after_id=None, limit=1000)


def folder(tmp_path, count=5):
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    for i in range(count):
        (root / f"n{i}.md").write_text(f"# Note {i}\nunique words number{i} here")
    (root / "sub" / "deep.txt").write_text("deep radar sonar words")
    return root


async def test_runs_to_the_end_with_progress_numbers(stack, tmp_path):
    svc = bulk(stack)
    run_id = await svc.start(folder(tmp_path), "org1", "alice", tags=("t/x",))
    first = await svc.status(run_id)
    assert first.state == "running" and first.progress.done == 0
    await drain(runner(stack, svc))
    status = await svc.status(run_id)
    p = status.progress
    assert status.state == "done" and (p.done, p.failed, p.skipped) == (6, 0, 0)
    assert p.created == 6 and p.bytes > 0 and p.cursor == "sub/deep.txt"
    assert len(await stored(stack)) == 6
    assert [s.run_id for s in await svc.list_runs("org1")] == [run_id]
    assert await svc.list_runs("other") == []


async def test_duplicate_run_creates_nothing(stack, tmp_path):
    svc, root = bulk(stack), folder(tmp_path)
    first = await svc.start(root, "org1", "alice")
    await drain(runner(stack, svc))
    second = await svc.start(root, "org1", "alice")
    await drain(runner(stack, svc))
    again = (await svc.status(second)).progress
    assert (await svc.status(first)).progress.created == 6
    assert (again.done, again.created) == (6, 0) and len(await stored(stack)) == 6


async def test_resume_after_restart_mid_folder(stack, tmp_path):
    root = folder(tmp_path, 7)
    svc = bulk(stack)
    run_id = await svc.start(root, "org1", "alice")
    first = runner(stack, svc)
    assert await first.run_once() and await first.run_once()  # two steps, then "the server dies"
    mid = await svc.status(run_id)
    assert mid.state == "running" and mid.progress.done == 4 and len(await stored(stack)) == 4
    restarted = bulk(stack)  # a new process: only the database remembers
    await drain(runner(stack, restarted, "w2"))
    end = await restarted.status(run_id)
    assert end.state == "done" and end.progress.done == 8 and len(await stored(stack)) == 8


async def test_crash_before_next_step_is_queued_is_repaired(stack, tmp_path):
    svc = bulk(stack)
    run_id = await svc.start(folder(tmp_path), "org1", "alice")
    job = await stack.backend.jobs.claim("w1", frozenset({INGEST_JOB}), 60)
    original = svc._enqueue

    async def dying(*a, **k):
        raise RuntimeError("killed")

    svc._enqueue = dying
    with pytest.raises(RuntimeError):
        await svc.handle_step(job)  # the step record is written, the next job is not
    svc._enqueue = original
    await stack.backend.jobs.fail(job.id, "w1", "killed", 0)
    await drain(runner(stack, svc))
    assert (await svc.status(run_id)).progress.done == 6


async def test_two_workers_on_the_same_step_count_once(stack, tmp_path):
    svc = bulk(stack, step_files=3)
    run_id = await svc.start(folder(tmp_path), "org1", "alice")
    job = await stack.backend.jobs.claim("w1", frozenset({INGEST_JOB}), 60)
    await asyncio.gather(svc.handle_step(job), svc.handle_step(job))  # a reclaimed lease
    mid = (await svc.status(run_id)).progress
    assert mid.done == 3 and len(await stored(stack)) == 3
    await stack.backend.jobs.complete(job.id, "w1")
    await asyncio.gather(
        *(drain(runner(stack, svc, f"w{i}")) for i in range(2, 5))
    )  # three racing runners
    end = await svc.status(run_id)
    assert end.state == "done" and end.progress.done == 6 and len(await stored(stack)) == 6


async def test_cancel_stops_and_keeps_what_was_stored(stack, tmp_path):
    svc = bulk(stack)
    run_id = await svc.start(folder(tmp_path), "org1", "alice")
    run = runner(stack, svc)
    await run.run_once()
    assert await svc.cancel(run_id)
    assert (await svc.status(run_id)).state == "cancelling"
    await drain(run)
    end = await svc.status(run_id)
    assert end.state == "cancelled" and end.progress.done == 2 and len(await stored(stack)) == 2
    assert not await svc.cancel(run_id) and not await svc.cancel("nope")


async def test_a_bad_file_is_recorded_and_the_run_goes_on(stack, tmp_path):
    root = folder(tmp_path, 3)
    (root / "bad.bin").write_bytes(b"\x00\x01")
    (root / "empty.txt").write_text("   ")
    svc = bulk(stack)
    run_id = await svc.start(root, "org1", "alice")
    await drain(runner(stack, svc))
    end = await svc.status(run_id)
    assert end.state == "done" and end.progress.done == 4 and end.progress.failed == 2
    assert any(e.startswith("bad.bin:") for e in end.progress.errors)


async def test_error_list_is_bounded_but_counters_are_not(stack, tmp_path):
    root = tmp_path / "many"
    root.mkdir()
    for i in range(30):
        (root / f"f{i:02d}.bin").write_bytes(b"\x00")
    svc = bulk(stack, step_files=10)
    run_id = await svc.start(root, "org1", "alice")
    await drain(runner(stack, svc))
    p = (await svc.status(run_id)).progress
    assert p.failed == 30 and len(p.errors) == 20


async def test_symlinks_and_hidden_files_are_not_ingested(stack, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret words")
    root = folder(tmp_path, 1)
    (root / "evil").symlink_to(outside, target_is_directory=True)
    (root / "evil.txt").symlink_to(outside / "secret.txt")
    (root / ".hidden.txt").write_text("hidden words")
    svc = bulk(stack)
    run_id = await svc.start(root, "org1", "alice")
    await drain(runner(stack, svc))
    assert (await svc.status(run_id)).progress.done == 2
    assert all("secret" not in i.body and "hidden" not in i.body for i in await stored(stack))


async def test_folder_checks_and_allowed_root(stack, tmp_path):
    root = folder(tmp_path)
    svc = bulk(stack, allowed_root=root)
    with pytest.raises(ValidationError, match="not found"):
        await svc.start(tmp_path / "missing", "org1", "alice")
    with pytest.raises(ValidationError, match="not a directory"):
        await svc.start(root / "n0.md", "org1", "alice")
    with pytest.raises(ValidationError, match="outside"):
        await svc.start(tmp_path, "org1", "alice")
    escape = root / "up"
    escape.symlink_to(tmp_path, target_is_directory=True)  # a link out is resolved, then refused
    with pytest.raises(ValidationError, match="outside"):
        await svc.start(escape, "org1", "alice")
    await svc.start(root / "sub", "org1", "alice")  # below the root: accepted


async def test_folder_vanishing_fails_the_run_cleanly(stack, tmp_path):
    root = folder(tmp_path)
    svc = bulk(stack)
    run_id = await svc.start(root, "org1", "alice")
    for p in sorted(root.rglob("*"), reverse=True):
        p.unlink() if not p.is_dir() else p.rmdir()
    root.rmdir()
    await drain(runner(stack, svc))
    end = await svc.status(run_id)
    assert end.state == "failed" and "not found" in end.progress.errors[0]


async def test_over_limit_files_text_in_pieces_others_skipped(stack, tmp_path, monkeypatch):
    monkeypatch.setattr(bulk_files, "MAX_FILE_BYTES", 2000)
    monkeypatch.setattr(bulk_files, "PIECE_BYTES", 1000)
    monkeypatch.setattr(bulk_files, "BULK_TEXT_MAX_BYTES", 20000)
    root = tmp_path / "big"
    root.mkdir()
    (root / "log.txt").write_text("".join(f"line {i} of the big log\n" for i in range(200)))
    (root / "huge.txt").write_text("x " * 20000)
    (root / "scan.pdf").write_bytes(b"%PDF-1.4" + b"0" * 3000)
    (root / "blob").write_bytes(b"\x00" * 3000)
    svc = bulk(stack)
    run_id = await svc.start(root, "org1", "alice")
    await drain(runner(stack, svc))
    p = (await svc.status(run_id)).progress
    assert (p.done, p.skipped, p.failed) == (1, 2, 1)
    assert any("scan.pdf" in e and "limit" in e for e in p.errors)
    titles = sorted(i.title for i in await stored(stack))
    assert len(titles) >= 5 and all(t.startswith("log.part") for t in titles)
    body = "".join(i.body for i in sorted(await stored(stack), key=lambda i: i.title))
    assert body.count("line ") == 200  # nothing lost between pieces
    await drain(runner(stack, svc))
    again = await svc.start(root, "org1", "alice")
    await drain(runner(stack, svc))
    assert (await svc.status(again)).progress.created == 0
