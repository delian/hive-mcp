"""The one-container promise, end to end with the real `hive serve` process: a note survives the
container being replaced (empty local disk, same backup share), and two instances never overlap."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import httpx
import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from hive_mcp.ops.autobackup import archives
from hive_mcp.security.tokens import TokenStore
from tests.conformance.factory import ALICE
from tests.integration.util import free_port

if TYPE_CHECKING:
    from pathlib import Path

START = "from hive_mcp.cli import main; main()"


class Instance:
    def __init__(self, data: Path, backups: Path, port: int, **env: str) -> None:
        self.data, self.port = data, port
        self.env = {
            **os.environ,
            "HIVE_DATA_DIR": str(data), "HIVE_BACKUP_DIR": str(backups), "HIVE_PORT": str(port),
            "HIVE_HOST": "127.0.0.1", "HIVE_PUBLIC_URL": f"http://127.0.0.1:{port}",
            "HIVE_BACKUP_EVERY_MINUTES": "1", "HIVE_DRAIN_SECONDS": "0", "HIVE_BACKUP_LEASE_WAIT": "3",
            **env,
        }  # fmt: skip
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen([sys.executable, "-c", START, "serve"], env=self.env)  # noqa: S603
        for _ in range(300):
            try:
                if httpx.get(f"http://127.0.0.1:{self.port}/readyz", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited with {self.proc.returncode}")
            time.sleep(0.1)
        raise RuntimeError("server did not become ready")

    def stop(self) -> int:
        assert self.proc
        self.proc.send_signal(signal.SIGTERM)
        code = self.proc.wait(timeout=60)
        return 0 if code in (0, -signal.SIGTERM) else code  # uvicorn re-raises SIGTERM when done

    def client(self, token: str) -> Client:
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
        return Client(streamable_http_client(f"http://127.0.0.1:{self.port}/mcp", http_client=http))


async def test_a_replaced_container_comes_back_with_its_data(tmp_path: Path) -> None:
    backups = tmp_path / "share"
    first = Instance(tmp_path / "disk1", backups, free_port())
    token = TokenStore(first.data / "tokens.json").create(ALICE, label="alice")
    first.start()
    try:
        async with first.client(token) as c:
            made = (
                await c.call_tool(
                    "kb_remember", {"title": "Runbook", "body": "restart the ingest worker"}
                )
            ).structured_content
    finally:
        assert first.stop() == 0  # a clean stop takes the final backup
    assert len(archives(backups)) >= 1 and not (backups / ".hive-lease").exists()

    shutil.rmtree(first.data)  # the container is gone, and its disk with it
    second = Instance(tmp_path / "disk2", backups, free_port())
    second.start()
    try:
        # the token file came back with the backup (it is part of the archive)
        restored_token = TokenStore(second.data / "tokens.json").list()
        assert [r.label for r in restored_token] == ["alice"]
        async with second.client(token) as c:
            got = (await c.call_tool("kb_get", {"id": made["id"]})).structured_content
            hits = (await c.call_tool("kb_consult", {"query": "ingest worker"})).structured_content
        assert got["title"] == "Runbook" and [r["id"] for r in hits["results"]] == [made["id"]]
    finally:
        assert second.stop() == 0


def test_a_second_instance_cannot_start_while_the_first_one_is_running(tmp_path: Path) -> None:
    backups = tmp_path / "share"
    first = Instance(tmp_path / "disk1", backups, free_port())
    first.start()
    try:
        rival = Instance(tmp_path / "disk2", backups, free_port())
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-c", START, "serve"],
            env=rival.env,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode != 0
        assert b"another instance" in proc.stderr
        assert not (tmp_path / "disk2" / "hive.db").exists()  # it never touched its own database
    finally:
        assert first.stop() == 0


@pytest.mark.skipif(sys.platform == "win32", reason="signals")
def test_after_a_crash_the_next_instance_waits_for_the_lease_to_go_stale(tmp_path: Path) -> None:
    from hive_mcp.ops import lease

    backups = tmp_path / "share"
    first = Instance(tmp_path / "disk1", backups, free_port())
    first.start()
    assert first.proc
    first.proc.kill()  # no clean shutdown: the lease file stays behind
    first.proc.wait()
    old = time.time() - lease.STALE_SECONDS - 5
    os.utime(backups / ".hive-lease", (old, old))  # time passes
    second = Instance(tmp_path / "disk2", backups, free_port())
    second.start()  # takes over the abandoned lease
    assert second.stop() == 0
