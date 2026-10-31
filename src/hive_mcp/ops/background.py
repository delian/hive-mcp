"""The job runner and the other background loops in a thread of their own.

The thread has its own event loop and its own storage connections (it builds its own services), so
a busy request loop cannot hold the jobs back, and a long job cannot hold a request. It is still one
process: the interpreter lock is shared, so this separates the *scheduling*, not the CPU.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hive_mcp.ops.loops import start_loops

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from hive_mcp.bootstrap import Services
    from hive_mcp.config import Settings

log = logging.getLogger("hive.app")
START_SECONDS = 60.0
STOP_SECONDS = 60.0


class BackgroundWorker:
    def __init__(
        self, settings: Settings, factory: Callable[[Settings], Awaitable[Services]]
    ) -> None:
        self._settings, self._factory = settings, factory
        self._ready, self._stopped = threading.Event(), threading.Event()
        self._failure: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._main, name="hive-background", daemon=True)

    def start(self, timeout: float = START_SECONDS) -> bool:
        """True when the worker is running; False when it could not start (the caller then runs
        the jobs on its own loop, as without a worker)."""
        self._thread.start()
        self._ready.wait(timeout)
        return self._ready.is_set() and self._failure is None

    def stop(self, timeout: float = STOP_SECONDS) -> None:
        if self._loop is not None and self._stop is not None:
            with contextlib.suppress(RuntimeError):  # the loop is already gone
                self._loop.call_soon_threadsafe(self._stop.set)
        self._stopped.wait(timeout)
        self._thread.join(1.0)

    def _main(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:  # reported to start(), never raised in a thread
            self._failure = exc
            log.exception("background.failed")
        finally:
            self._ready.set()
            self._stopped.set()

    async def _serve(self) -> None:
        services = await self._factory(self._settings)
        self._loop, self._stop = asyncio.get_running_loop(), asyncio.Event()
        try:
            tasks: list[asyncio.Task[None]] = []
            if self._settings.run_jobs:
                tasks.append(asyncio.create_task(services.runner.run_forever()))
            tasks += start_loops(services, self._settings)  # the backup loop is last by design
            self._ready.set()
            await self._stop.wait()
            for task in tasks:  # in order: the final backup must see every writer already stopped
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        finally:
            await services.close()


@dataclass
class Background:
    """What runs behind the server: in a worker thread, or as tasks on the caller's loop."""

    tasks: list[asyncio.Task[None]] = field(default_factory=list)
    worker: BackgroundWorker | None = None

    async def stop(self) -> None:
        for task in self.tasks:  # in order: the backup loop is last, so it sees the others stopped
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self.worker is not None:
            await asyncio.to_thread(self.worker.stop)


async def start_background(
    settings: Settings,
    services: Services,
    factory: Callable[[Settings], Awaitable[Services]],
    *,
    default_factory: Callable[[Settings], Awaitable[Services]],
) -> Background:
    """The worker thread when enabled (and the services are the real ones, which it can rebuild),
    else tasks on the running loop."""
    if settings.background_thread and factory is default_factory:
        worker = BackgroundWorker(settings, factory)
        if await asyncio.to_thread(worker.start):
            return Background(worker=worker)
        log.error("the background thread did not start; running the jobs on the request loop")
    tasks = [asyncio.create_task(services.runner.run_forever())] if settings.run_jobs else []
    return Background(tasks=[*tasks, *start_loops(services, settings)])
