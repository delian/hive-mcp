"""Graceful shutdown: fail readiness first, keep serving, then let uvicorn finish in-flight work."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import uvicorn

if TYPE_CHECKING:
    from types import FrameType

    from hive_mcp.ops.state import Lifecycle


class DrainingServer(uvicorn.Server):
    """On the first SIGTERM/SIGINT flip /readyz to 503, wait `grace` seconds, then shut down.

    The wait lets a load balancer notice the failing readiness probe and stop routing new
    requests; requests that still arrive are served. A second signal exits immediately.
    """

    def __init__(self, config: uvicorn.Config, lifecycle: Lifecycle, grace: float) -> None:
        super().__init__(config)
        self._lifecycle = lifecycle
        self._grace = grace
        self._timer: threading.Timer | None = None

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if self._lifecycle.draining or self._grace <= 0:
            if self._timer is not None:
                self._timer.cancel()
            super().handle_exit(sig, frame)
            return
        self._lifecycle.start_draining()
        self._timer = threading.Timer(self._grace, super().handle_exit, args=(sig, frame))
        self._timer.daemon = True
        self._timer.start()
