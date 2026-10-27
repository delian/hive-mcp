"""The backup-directory lease with many contenders: threads and processes race to acquire it,
exactly one holds it at any moment, a stale lease is taken over once, and a hand-off between
revisions with many starters ends with one new holder and the old revision retired."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

import pytest

from hive_mcp.domain.errors import ValidationError
from hive_mcp.ops import lease as lease_module
from hive_mcp.ops.lease import FILE, HANDOFF_FILE, RETIRED_FILE, Lease
from tests.stress.support import SCALE

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.stress


def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lease_module, "WATCH_SECONDS", 0.05)
    monkeypatch.setattr(lease_module, "POLL_SECONDS", 0.05)


def owner_on_disk(directory: Path) -> str | None:
    try:
        return json.loads((directory / FILE).read_text())["owner"]
    except (OSError, ValueError, KeyError):
        return None


class Racer(threading.Thread):
    """Tries to take the lease at a common starting line; records win or the refusal."""

    def __init__(  # noqa: PLR0913, PLR0917 - a test helper with options
        self,
        directory: Path,
        name: str,
        start_line: threading.Barrier,
        wait: float = 0,
        revision: str | None = None,
        on_handoff=None,
    ) -> None:
        super().__init__(daemon=True)
        self.lease = Lease(directory, name, revision=revision, on_handoff=on_handoff)
        self.start_line, self.wait = start_line, wait
        self.won = False
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.start_line.wait(10)
            self.lease.acquire(self.wait)
            self.won = True
        except ValidationError as exc:
            self.error = exc
        except BaseException as exc:  # a crash is worse than a refusal: keep it for the assert
            self.error = exc


def race(directory: Path, count: int, **kw) -> list[Racer]:
    line = threading.Barrier(count)
    racers = [Racer(directory, f"{i:02d}-{os.urandom(4).hex()}", line, **kw) for i in range(count)]
    for r in racers:
        r.start()
    for r in racers:
        r.join(60)
    assert not any(r.is_alive() for r in racers)
    return racers


def test_threads_racing_for_a_free_lease_produce_exactly_one_holder(tmp_path: Path) -> None:
    for round_no in range(3 * SCALE):
        racers = race(tmp_path, 16)
        crashed = [r.error for r in racers if r.error and not isinstance(r.error, ValidationError)]
        assert not crashed, crashed
        winners = [r for r in racers if r.won]
        assert len(winners) == 1, f"round {round_no}: {len(winners)} holders"
        assert owner_on_disk(tmp_path) == winners[0].lease.owner
        for loser in (r for r in racers if not r.won):
            assert "another instance" in str(loser.error)
        winners[0].lease.release()
        assert not (tmp_path / FILE).exists()
        assert not list(tmp_path.glob(f"{FILE}.*")), "half-written leftovers"


def test_racing_for_a_stale_lease_takes_it_over_exactly_once(tmp_path: Path) -> None:
    for _ in range(2 * SCALE):
        (tmp_path / FILE).write_text(json.dumps({"owner": "dead-instance"}))
        long_ago = time.time() - lease_module.STALE_SECONDS - 30
        os.utime(tmp_path / FILE, (long_ago, long_ago))
        racers = race(tmp_path, 12)
        winners = [r for r in racers if r.won]
        assert len(winners) == 1 and owner_on_disk(tmp_path) == winners[0].lease.owner
        assert all(r.error is None or isinstance(r.error, ValidationError) for r in racers)
        winners[0].lease.release()


def test_a_beating_holder_is_never_taken_over_even_when_the_file_looks_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast(monkeypatch)
    monkeypatch.setattr(lease_module, "HEARTBEAT_SECONDS", 0.05)
    holder = Lease(tmp_path, "holder")
    holder.acquire(0)
    try:
        stop = time.monotonic() + 1.0
        rounds = 0
        while time.monotonic() < stop:
            old = time.time() - lease_module.STALE_SECONDS - 10
            os.utime(tmp_path / FILE, (old, old))  # a clock jump / slow disk: the file looks stale
            time.sleep(0.12)  # the heartbeat refreshes it well within this
            rounds += 1
        racers = race(tmp_path, 8)
        assert not any(r.won for r in racers)
        assert owner_on_disk(tmp_path) == "holder" and rounds > 3
    finally:
        holder.release()


def test_waiters_take_turns_and_there_is_never_more_than_one_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast(monkeypatch)
    count = 6
    inside, peak, order = 0, 0, []
    guard = threading.Lock()
    line = threading.Barrier(count)
    errors: list[BaseException] = []

    def contender(name: str) -> None:
        nonlocal inside, peak
        lease = Lease(tmp_path, name)
        try:
            line.wait(10)
            lease.acquire(60)
            with guard:
                inside += 1
                peak = max(peak, inside)
                order.append(name)
            assert owner_on_disk(tmp_path) == name
            time.sleep(0.05)
            with guard:
                inside -= 1
            lease.release()
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=contender, args=(f"c{i}-{os.urandom(3).hex()}",))
        for i in range(count)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(90)
    assert not errors, errors
    assert sorted(order) == sorted(set(order)) and len(order) == count  # everyone got a turn
    assert peak == 1, "two instances held the lease at once"
    assert not (tmp_path / FILE).exists()


# ---------------------------------------------------------------------------------- processes
CHILD = """
import sys, time
from pathlib import Path
from hive_mcp.domain.errors import ValidationError
from hive_mcp.ops.lease import Lease
directory, go, name = Path(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
while time.time() < go:
    time.sleep(0.001)
lease = Lease(directory, name)
try:
    lease.acquire(0)
except ValidationError:
    print("LOST", name, flush=True)
    sys.exit(0)
print("WON", name, flush=True)
release = directory / "release-now"
deadline = time.time() + 60
while not release.exists() and time.time() < deadline:
    time.sleep(0.02)
lease.release()
"""


def test_processes_racing_for_the_lease_produce_exactly_one_holder(tmp_path: Path) -> None:
    count = 8
    directory = tmp_path / "share"
    directory.mkdir()
    go = time.time() + 3.0  # all children are imported and waiting by then
    procs = [
        subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-I",
                "-c",
                CHILD,
                str(directory),
                str(go),
                f"proc{i}-{os.urandom(3).hex()}",
            ],
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ},
        )
        for i in range(count)
    ]
    try:
        deadline = time.time() + 40
        while sum(p.poll() is not None for p in procs) < count - 1 and time.time() < deadline:
            time.sleep(0.05)
        exited = [p for p in procs if p.poll() is not None]
        survivors = [p for p in procs if p.poll() is None]
        assert len(survivors) == 1 and len(exited) == count - 1, "exactly one process holds it"
        assert all((p.stdout.read() if p.stdout else "").startswith("LOST") for p in exited)
        assert survivors[0].stdout and survivors[0].stdout.readline().startswith("WON")
        holder = owner_on_disk(directory)
        assert holder is not None and holder.startswith("proc")
    finally:
        (directory / "release-now").write_text("x")
        for p in procs:
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
            if p.stdout:
                p.stdout.close()
    assert not (directory / FILE).exists(), "the winner released it on exit"


# --------------------------------------------------------------------------------- hand-off
def test_many_starters_of_a_new_revision_one_wins_and_the_old_revision_is_retired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast(monkeypatch)
    yielded = threading.Event()
    holder_box: dict[str, Lease] = {}

    def retire() -> None:  # what the server does when asked: final backup, release, exit
        holder_box["old"].release()
        yielded.set()

    old = Lease(tmp_path, "old", revision="r1", on_handoff=retire)
    holder_box["old"] = old
    old.acquire(0)
    count = 7
    line = threading.Barrier(count + 3)
    new = [Racer(tmp_path, f"new{i}", line, wait=2, revision="r2") for i in range(count)]
    again = [Racer(tmp_path, f"s{i}", line, wait=0.5, revision="r2") for i in range(3)]
    everyone = new + again
    for r in everyone:
        r.start()
    for r in everyone:
        r.join(60)
    assert yielded.is_set()
    winners = [r for r in everyone if r.won]
    assert len(winners) == 1, [(r.lease.owner, repr(r.error)[:80]) for r in everyone]
    assert owner_on_disk(tmp_path) == winners[0].lease.owner
    assert "r1" in json.loads((tmp_path / RETIRED_FILE).read_text())
    assert not (tmp_path / HANDOFF_FILE).exists()
    for r in everyone:
        if r is not winners[0]:
            assert isinstance(r.error, ValidationError), r.error
    winners[0].lease.release()
    with pytest.raises(ValidationError, match="handed over"):  # the platform restarts the old one
        Lease(tmp_path, "old-restarted", revision="r1").acquire(0)


def test_a_chain_of_revisions_each_hands_over_to_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast(monkeypatch)
    revisions = [f"r{i}" for i in range(1, 4 + SCALE)]
    holders: list[Lease] = []

    def start(rev: str) -> Lease:
        box: dict[str, Lease] = {}
        lease = Lease(tmp_path, f"inst-{rev}", revision=rev, on_handoff=lambda: box["l"].release())  # noqa: PLW0108 - box is filled below
        box["l"] = lease
        lease.acquire(15)
        holders.append(lease)
        return lease

    start(revisions[0])
    for rev in revisions[1:]:
        start(rev)  # asks the previous holder to yield and takes over
        assert owner_on_disk(tmp_path) == f"inst-{rev}"
    retired = json.loads((tmp_path / RETIRED_FILE).read_text())
    assert set(retired) == set(revisions[:-1])
    for rev in revisions[:-1]:
        with pytest.raises(ValidationError, match="handed over"):
            Lease(tmp_path, f"again-{rev}", revision=rev).acquire(0)
    holders[-1].release()
    assert not (tmp_path / FILE).exists()


@pytest.mark.xfail(
    strict=True,
    reason="BUG-STRESS-1: an instance of a retired revision that is already waiting in "
    "Lease.acquire() takes the lease the holder just yielded; the retired check runs only once, "
    "before the wait loop (src/hive_mcp/ops/lease.py:103-135)",
)
def test_a_waiting_instance_of_the_yielding_revision_must_not_take_the_lease_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast(monkeypatch)
    box: dict[str, Lease] = {}
    yielded = threading.Event()

    def retire() -> None:
        box["old"].release()
        yielded.set()

    box["old"] = Lease(tmp_path, "old", revision="r1", on_handoff=retire)
    box["old"].acquire(0)
    waiting = Racer(tmp_path, "old-restarted", threading.Barrier(1), wait=5, revision="r1")
    waiting.start()  # the platform restarted the old revision's container: it waits for the lease
    time.sleep(0.4)
    (tmp_path / HANDOFF_FILE).write_text(json.dumps({"revision": "r2", "owner": "new"}))
    assert yielded.wait(5)
    waiting.join(10)
    try:
        assert "r1" in json.loads((tmp_path / RETIRED_FILE).read_text())
        assert not waiting.won, "a retired revision took the lease back"
    finally:
        if waiting.won:
            waiting.lease.release()


@pytest.mark.xfail(
    strict=True,
    reason="BUG-STRESS-2: the half-written lease file is named after owner[:8], so two owners with "
    "the same 8-character prefix share it and acquire() dies with FileNotFoundError "
    "(src/hive_mcp/ops/lease.py:78-80)",
)
def test_owners_sharing_a_name_prefix_do_not_trample_each_others_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = Lease(tmp_path, "instance-a"), Lease(tmp_path, "instance-b")
    tmp_path.mkdir(exist_ok=True)
    real = type(tmp_path).write_text
    fired: list[int] = []

    def interleaved(
        self, *args, **kwargs
    ):  # the second starter writes between our write and rename
        result = real(self, *args, **kwargs)
        if self.name.startswith(FILE + ".") and not fired:
            fired.append(1)
            second._write()
        return result

    monkeypatch.setattr(type(tmp_path), "write_text", interleaved)
    first._write()  # raises FileNotFoundError: its temp file was already renamed by the other
    assert owner_on_disk(tmp_path) == "instance-a"
