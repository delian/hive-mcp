import json
import os
import time

import pytest

from hive_mcp.domain.errors import ValidationError
from hive_mcp.ops import lease as lease_module
from hive_mcp.ops.lease import FILE, Lease


def test_the_first_instance_gets_the_lease_and_a_second_is_refused_while_it_is_alive(tmp_path):
    first, second = Lease(tmp_path, "one"), Lease(tmp_path, "two")
    first.acquire(0)
    try:
        assert json.loads((tmp_path / FILE).read_text())["owner"] == "one"
        with pytest.raises(ValidationError, match="another instance"):
            second.acquire(0)
        assert json.loads((tmp_path / FILE).read_text())["owner"] == "one"
    finally:
        first.release()
    assert not (tmp_path / FILE).exists()  # a clean stop hands it over at once
    second.acquire(0)
    second.release()


def test_a_waiting_instance_takes_over_when_the_holder_releases(tmp_path, monkeypatch):
    monkeypatch.setattr(lease_module, "POLL_SECONDS", 0.05)
    first = Lease(tmp_path, "one")
    first.acquire(0)
    import threading

    threading.Timer(0.4, first.release).start()
    second = Lease(tmp_path, "two")
    started = time.monotonic()
    second.acquire(5)
    try:
        assert 0.3 < time.monotonic() - started < 3
        assert json.loads((tmp_path / FILE).read_text())["owner"] == "two"
    finally:
        second.release()


def test_a_lease_nobody_touches_any_more_is_taken_over(tmp_path):
    (tmp_path / FILE).write_text(json.dumps({"owner": "dead"}))
    old = time.time() - lease_module.STALE_SECONDS - 5
    os.utime(tmp_path / FILE, (old, old))
    mine = Lease(tmp_path, "me")
    mine.acquire(0)
    try:
        assert json.loads((tmp_path / FILE).read_text())["owner"] == "me"
    finally:
        mine.release()


def test_a_garbled_lease_file_counts_as_free_and_release_never_removes_someone_elses(tmp_path):
    (tmp_path / FILE).write_text("{not json")
    mine = Lease(tmp_path, "me")
    mine.acquire(0)
    (tmp_path / FILE).write_text(json.dumps({"owner": "intruder"}))
    mine.release()
    assert json.loads((tmp_path / FILE).read_text())["owner"] == "intruder"


def test_the_heartbeat_keeps_the_lease_fresh_and_stops_when_taken_over(tmp_path, monkeypatch):
    monkeypatch.setattr(lease_module, "HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(lease_module, "WATCH_SECONDS", 0.05)
    mine = Lease(tmp_path, "me")
    mine.acquire(0)
    try:
        old = time.time() - 30
        os.utime(tmp_path / FILE, (old, old))
        time.sleep(0.3)
        assert time.time() - (tmp_path / FILE).stat().st_mtime < 5  # refreshed by the beat
        (tmp_path / FILE).write_text(json.dumps({"owner": "other"}))
        time.sleep(0.2)
        assert json.loads((tmp_path / FILE).read_text())["owner"] == "other"  # we did not fight
    finally:
        mine.release()


# ---------------------------------------------------------------- hand-off between revisions
def fast(monkeypatch):
    monkeypatch.setattr(lease_module, "WATCH_SECONDS", 0.05)
    monkeypatch.setattr(lease_module, "POLL_SECONDS", 0.05)


def test_a_newer_revision_asks_the_holder_to_yield_and_the_holder_retires_itself(
    tmp_path, monkeypatch
):
    import threading

    fast(monkeypatch)
    yielded = threading.Event()
    old = Lease(tmp_path, "old", revision="r1", on_handoff=yielded.set)
    old.acquire(0)
    threading.Timer(
        0.3, lambda: (yielded.wait(5), old.release())
    ).start()  # what the server does on SIGTERM
    new = Lease(tmp_path, "new", revision="r2")
    new.acquire(10)
    try:
        assert yielded.is_set()
        assert json.loads((tmp_path / FILE).read_text())["owner"] == "new"
        assert "r1" in json.loads((tmp_path / lease_module.RETIRED_FILE).read_text())
        assert not (tmp_path / lease_module.HANDOFF_FILE).exists()  # answered requests are cleared
    finally:
        new.release()
    # the platform restarts the old container: it must not take the lease back
    with pytest.raises(ValidationError, match="handed over"):
        Lease(tmp_path, "old-again", revision="r1").acquire(0)


def test_the_same_revision_or_no_revision_never_yields(tmp_path, monkeypatch):
    fast(monkeypatch)
    calls = []
    holder = Lease(tmp_path, "a", revision="r1", on_handoff=lambda: calls.append(1))
    holder.acquire(0)
    try:
        for rival in (Lease(tmp_path, "b", revision="r1"), Lease(tmp_path, "c")):
            with pytest.raises(ValidationError, match="another instance"):
                rival.acquire(0.4)
        time.sleep(0.2)
        assert calls == [] and json.loads((tmp_path / FILE).read_text())["owner"] == "a"
        assert not (tmp_path / lease_module.HANDOFF_FILE).exists()
    finally:
        holder.release()


def test_a_holder_without_a_handoff_callback_ignores_requests(tmp_path, monkeypatch):
    fast(monkeypatch)
    holder = Lease(tmp_path, "a", revision="r1")  # hand-off not enabled for it
    holder.acquire(0)
    try:
        with pytest.raises(ValidationError):
            Lease(tmp_path, "b", revision="r2").acquire(0.4)
        assert json.loads((tmp_path / FILE).read_text())["owner"] == "a"
        assert not (tmp_path / lease_module.RETIRED_FILE).exists()
    finally:
        holder.release()


def test_old_retirements_are_forgotten_after_a_day(tmp_path):
    (tmp_path / lease_module.RETIRED_FILE).write_text(
        json.dumps({"r-old": time.time() - 2 * 86_400, "r-new": time.time()})
    )
    Lease(tmp_path, "x", revision="r-old").acquire(0)  # long forgotten: allowed again
    with pytest.raises(ValidationError, match="handed over"):
        Lease(tmp_path, "y", revision="r-new").acquire(0)


# ---------------------------------------------------------------- details people rely on
def test_the_lease_file_says_who_holds_it_and_which_revision(tmp_path):
    held = Lease(tmp_path / "deep" / "dir", "me", revision="r7")  # the directory is created
    held.acquire(0)
    try:
        record = json.loads((tmp_path / "deep" / "dir" / FILE).read_text())
        assert record == {"owner": "me", "pid": os.getpid(), "revision": "r7"}
        assert not list((tmp_path / "deep" / "dir").glob(f"{FILE}.*"))  # no half-written leftovers
    finally:
        held.release()


def test_the_refusal_tells_the_operator_what_to_do(tmp_path):
    first = Lease(tmp_path, "one")
    first.acquire(0)
    try:
        with pytest.raises(ValidationError) as caught:
            Lease(tmp_path, "two").acquire(0)
        assert str(caught.value) == (
            "another instance is using this backup directory (lease held); two instances "
            "must not run at once. Stop it first, or wait for it to finish shutting down"
        )
    finally:
        first.release()


def test_a_handoff_request_names_the_asker_and_is_not_rewritten_while_unanswered(tmp_path):
    first = Lease(tmp_path, "one", revision="r1")
    first.acquire(0)
    try:
        asker = Lease(tmp_path, "two", revision="r2")
        with pytest.raises(ValidationError):
            asker.acquire(0)
        request = tmp_path / lease_module.HANDOFF_FILE
        assert json.loads(request.read_text()) == {"revision": "r2", "owner": "two"}
        request.write_text(json.dumps({"revision": "r2", "owner": "marker"}))
        with pytest.raises(ValidationError):
            asker.acquire(0)
        assert json.loads(request.read_text())["owner"] == "marker"
    finally:
        first.release()


def test_a_lease_held_by_someone_else_and_fresh_is_not_taken_by_a_matching_revision_field(
    tmp_path,
):
    (tmp_path / FILE).write_text(json.dumps({"owner": "other", "revision": "me"}))
    with pytest.raises(ValidationError):  # the owner is compared, not the revision
        Lease(tmp_path, "me", revision="me").acquire(0)


def test_a_missing_lease_after_writing_is_not_ours(tmp_path, monkeypatch):
    mine = Lease(tmp_path, "me")
    monkeypatch.setattr(mine, "_read", lambda: None)  # nothing readable even after we wrote
    monkeypatch.setattr(lease_module.time, "sleep", lambda _s: None)
    with pytest.raises(ValidationError):
        mine.acquire(0)


def test_release_before_acquire_is_harmless_and_the_watcher_thread_stops(tmp_path):
    Lease(tmp_path, "never").release()
    held = Lease(tmp_path, "me")
    held.acquire(0)
    thread = held._thread
    assert thread is not None and thread.daemon and thread.name == "hive-lease"
    held.release()
    assert not thread.is_alive()


def test_releasing_from_the_watcher_thread_itself_does_not_wait_for_itself(tmp_path, monkeypatch):
    import threading

    fast(monkeypatch)
    done, errors = threading.Event(), []
    holder: dict[str, Lease] = {}

    def yield_now() -> None:
        try:
            holder["lease"].release()  # runs on the hive-lease thread, as a real hand-off might
        except RuntimeError as exc:
            errors.append(exc)
        done.set()

    holder["lease"] = Lease(tmp_path, "old", revision="r1", on_handoff=yield_now)
    holder["lease"].acquire(0)
    (tmp_path / lease_module.HANDOFF_FILE).write_text(json.dumps({"revision": "r2"}))
    assert done.wait(5) and errors == []
    assert not (tmp_path / FILE).exists()
