"""The background worker: its own thread and loop, ordered shutdown, and a fallback when it fails."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from hive_mcp.config import Settings
from hive_mcp.ops import background
from hive_mcp.ops.background import BackgroundWorker, start_background


def make_services(log):
    class Runner:
        async def run_forever(self):
            log.append(("runner", threading.current_thread().name, id(asyncio.get_running_loop())))
            try:
                await asyncio.Event().wait()
            finally:
                log.append(("runner-stopped",))

    async def close():
        log.append(("closed",))

    backend = SimpleNamespace(audit=SimpleNamespace(append=None), kv=None)
    return SimpleNamespace(
        runner=Runner(), close=close, digest_sender=None, backend=backend, connectors=None
    )


def settings(**kw):
    return Settings(background_thread=True, **kw)


def test_the_runner_lives_in_its_own_thread_and_loop_and_stops_in_order(tmp_path, monkeypatch):
    log: list[tuple] = []
    monkeypatch.setattr(background, "start_loops", lambda s, st: [])

    async def factory(_):
        return make_services(log)

    worker = BackgroundWorker(settings(data_dir=tmp_path), factory)
    assert worker.start(10)
    deadline = 50
    while not any(e[0] == "runner" for e in log) and deadline:
        threading.Event().wait(0.05)
        deadline -= 1
    name = next(e[1] for e in log if e[0] == "runner")
    assert name == "hive-background"
    worker.stop(10)
    assert [e[0] for e in log if e[0] != "runner"] == ["runner-stopped", "closed"]
    assert not any(t.name == "hive-background" and t.is_alive() for t in threading.enumerate())


async def test_a_worker_that_cannot_start_falls_back_to_the_request_loop(tmp_path, monkeypatch):
    log: list[tuple] = []
    monkeypatch.setattr(background, "start_loops", lambda s, st: [])

    async def broken(_):
        raise RuntimeError("no database")

    async def real(_):  # the services the request side already built
        return make_services(log)

    services = make_services(log)
    bg = await start_background(
        settings(data_dir=tmp_path), services, broken, default_factory=broken
    )
    assert bg.worker is None and len(bg.tasks) == 1  # the runner is a task on this loop
    await asyncio.sleep(0.05)
    assert log[0][1] != "hive-background"
    await bg.stop()
    assert ("runner-stopped",) in log


async def test_custom_services_and_a_disabled_setting_stay_on_the_request_loop(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(background, "start_loops", lambda s, st: [])
    log: list[tuple] = []

    async def default(_):
        raise AssertionError("must not build a second set of services")

    async def custom(_):
        raise AssertionError("must not build a second set of services")

    services = make_services(log)
    for cfg, factory in [
        (settings(data_dir=tmp_path), custom),  # a test double is not rebuilt
        (Settings(data_dir=tmp_path, background_thread=False), default),  # off
    ]:
        bg = await start_background(cfg, services, factory, default_factory=default)
        assert bg.worker is None and len(bg.tasks) == 1
        await bg.stop()


async def test_no_runner_when_jobs_are_switched_off(tmp_path, monkeypatch):
    monkeypatch.setattr(background, "start_loops", lambda s, st: [])

    async def f(_):
        return None

    bg = await start_background(
        Settings(data_dir=tmp_path, run_jobs=False, background_thread=False),
        make_services([]),
        f,
        default_factory=f,
    )
    assert bg.tasks == [] and bg.worker is None
