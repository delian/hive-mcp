"""Process lifecycle state shared by health endpoints and the drain logic."""

from __future__ import annotations

import threading


class Lifecycle:
    """Tracks whether the process still accepts new work."""

    def __init__(self) -> None:
        self._draining = threading.Event()

    @property
    def draining(self) -> bool:
        return self._draining.is_set()

    def start_draining(self) -> None:
        self._draining.set()
