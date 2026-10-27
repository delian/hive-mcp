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
