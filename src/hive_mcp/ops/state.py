"""Process lifecycle state shared by health endpoints and the drain logic."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class Lifecycle:
    """Tracks whether the process still accepts new work."""

    def __init__(self) -> None:
        self._draining = threading.Event()
        self._ready = threading.Event()
        # Asked by /readyz while serving: False when the storage the server depends on is gone.
        self.probe: Callable[[], Awaitable[bool]] | None = None

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def mark_ready(self) -> None:
        self._ready.set()

    @property
    def draining(self) -> bool:
        return self._draining.is_set()

    def start_draining(self) -> None:
        self._draining.set()
