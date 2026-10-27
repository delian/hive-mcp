"""Cross-area review findings (BUG-OPS-14, BUG-CLI-3)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace


from hive_mcp.ops import loops


async def test_bug_ops_14_connector_loop_survives_a_non_hive_error(monkeypatch, caplog) -> None:
    """A connector that raised anything but HiveError (a network error) killed the loop for good."""
    calls = []

    class Kv:
        async def put_if_absent(self, *a, **k):
            return True

    class Connectors:
        async def run(self, spec, connector):
            calls.append(1)
            raise OSError("network down")

    services = SimpleNamespace(backend=SimpleNamespace(kv=Kv()), connectors=Connectors())
    spec = SimpleNamespace(name="x", every_minutes=1)
    monkeypatch.setattr("hive_mcp.connectors.factory.make_connector", lambda s, st: object())
    monkeypatch.setattr(loops, "CONNECTOR_CHECK_SECONDS", 0.01)
    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(loops.connector_loop(services, None, [spec]))  # type: ignore[arg-type]
        await asyncio.sleep(0.15)
        assert not task.done()
        task.cancel()
    assert calls and "connector.failed" in caplog.text


def test_bug_cli_3_eval_set_file_is_private_even_when_it_existed(tmp_path) -> None:
    import os
    import stat

    out = tmp_path / "set.json"
    out.write_text("old")
    out.chmod(0o644)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    os.close(fd)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    src = Path("src/hive_mcp/cli_eval.py").read_text()
    assert "os.fchmod(fd, 0o600)" in src
