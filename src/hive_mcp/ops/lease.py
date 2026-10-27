"""One writer at a time: a lease file in the backup directory, with an optional hand-off.

With the data on each container's own disk, two containers alive at once (a platform starting the
new revision before stopping the old one) would write diverging databases, and the older one's last
changes would be lost. The running instance therefore holds a lease (a small file it touches every
few seconds); a second instance that finds a live lease waits for its release (a clean shutdown
deletes it, after the final backup) and gives up with a clear message if it does not come. A lease
nobody has touched for a while belongs to a dead instance and is taken over.

Hand-off (HIVE_BACKUP_HANDOFF, needs a revision id): waiting for a platform that only stops the old
instance once the new one is ready would never end, so a starting instance of a *different
revision* may ask the holder to yield. The holder takes its final backup, releases the lease, and
exits; the new instance then restores that backup and serves, a few seconds in total. The revision
that yielded is written to a retired list, so the platform restarting that old container does not
steal the lease back: a retired revision refuses to start.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
import uuid
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

log = logging.getLogger("hive.lease")
FILE = ".hive-lease"
HANDOFF_FILE = ".hive-handoff"
RETIRED_FILE = ".hive-retired"
HEARTBEAT_SECONDS = 10.0
WATCH_SECONDS = 1.0
STALE_SECONDS = 45.0
POLL_SECONDS = 2.0
RETIRED_KEEP_SECONDS = 86_400.0


class Lease:
    def __init__(
        self,
        directory: Path,
        owner: str | None = None,
        *,
        revision: str | None = None,
        on_handoff: Callable[[], None] | None = None,
    ) -> None:
        self._dir = directory
        self._path = directory / FILE
        self.owner = owner or uuid.uuid4().hex
        self.revision = revision  # None: this instance never asks for, or yields to, a hand-off
        self._on_handoff = on_handoff
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None  # pragma: no mutate

    # ------------------------------------------------------------------ files
    def _read(self) -> tuple[str, float, str | None] | None:
        try:
            data = json.loads(self._path.read_text())
            return str(data["owner"]), self._path.stat().st_mtime, data.get("revision")
        except (OSError, ValueError, KeyError, TypeError):
            return None  # absent or unreadable: nobody holds it

    def _write(self) -> None:
        partial = self._path.with_name(f"{FILE}.{self.owner[:8]}")  # pragma: no mutate
        record = {"owner": self.owner, "pid": os.getpid(), "revision": self.revision}
        partial.write_text(json.dumps(record))
        partial.replace(self._path)

    def _retired(self) -> dict[str, float]:
        try:
            data = json.loads((self._dir / RETIRED_FILE).read_text())
            now = time.time()
            return {
                k: float(v) for k, v in data.items() if now - float(v) < RETIRED_KEEP_SECONDS
            }  # pragma: no mutate
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _retire(self, revision: str) -> None:
        retired = {**self._retired(), revision: time.time()}
        (self._dir / RETIRED_FILE).write_text(json.dumps(retired))

    def _handoff_request(self) -> str | None:
        """The revision asking us to yield, if any."""
        try:
            return str(json.loads((self._dir / HANDOFF_FILE).read_text())["revision"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    # ------------------------------------------------------------------ taking it
    def acquire(self, wait_seconds: float) -> None:
        """Take the lease, waiting up to `wait_seconds` for a live holder to let go."""
        self._dir.mkdir(parents=True, exist_ok=True)
        if self.revision and self.revision in self._retired():
            raise ValidationError(
                f"revision {self.revision} was handed over to a newer one and must not start again"
            )
        deadline = time.monotonic() + wait_seconds
        announced = False  # pragma: no mutate
        while True:
            held = self._read()
            stale = held is not None and time.time() - held[1] > STALE_SECONDS  # pragma: no mutate
            if held is None or held[0] == self.owner or stale:
                self._write()
                # two starters at once: the last writer wins, and the loser sees it
                time.sleep(0.2)  # pragma: no mutate
                current = self._read()
                if current and current[0] == self.owner:
                    (self._dir / HANDOFF_FILE).unlink(missing_ok=True)
                    self._start_watching()
                    return
            elif self.revision and held[2] and held[2] != self.revision:
                self._ask_to_yield()
            if time.monotonic() >= deadline:  # pragma: no mutate
                raise ValidationError(
                    "another instance is using this backup directory (lease held); two instances "
                    "must not run at once. Stop it first, or wait for it to finish shutting down"
                )
            if not announced:  # pragma: no mutate
                log.warning(
                    "waiting for the running instance to release the lease"
                )  # pragma: no mutate
                announced = True  # pragma: no mutate
            time.sleep(POLL_SECONDS)

    def _ask_to_yield(self) -> None:
        request = self._dir / HANDOFF_FILE
        if self._handoff_request() != self.revision:
            request.write_text(json.dumps({"revision": self.revision, "owner": self.owner}))
            log.warning(
                "asked the running instance to hand over (revision %s)", self.revision
            )  # pragma: no mutate

    # ------------------------------------------------------------------ holding it
    def _start_watching(self) -> None:
        def watch() -> None:
            last_write = time.monotonic()  # pragma: no mutate
            while not self._stop.wait(WATCH_SECONDS):
                held = self._read()
                if held is not None and held[0] != self.owner:
                    log.error("the lease was taken over by another instance")  # pragma: no mutate
                    return
                asker = self._handoff_request()
                if asker and self.revision and asker != self.revision and self._on_handoff:
                    log.warning("handing over to revision %s", asker)  # pragma: no mutate
                    self._retire(self.revision)
                    self._on_handoff()
                    return
                if time.monotonic() - last_write >= HEARTBEAT_SECONDS:  # pragma: no mutate
                    with contextlib.suppress(OSError):  # pragma: no mutate
                        self._write()
                    last_write = time.monotonic()  # pragma: no mutate

        self._thread = threading.Thread(
            target=watch, name="hive-lease", daemon=True
        )  # pragma: no mutate
        self._thread.start()

    def release(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)  # pragma: no mutate
        held = self._read()
        if held is not None and held[0] == self.owner:
            self._path.unlink(missing_ok=True)  # pragma: no mutate - a vanished file is fine
