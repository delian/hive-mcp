"""A real `hive serve` process must exit cleanly on SIGTERM after its drain window."""

import os
import signal
import subprocess
import sys
import time

import httpx

from tests.integration.conftest import free_port


def test_sigterm_flips_readiness_then_process_exits(tmp_path) -> None:
    port = free_port()
    env = {
        **os.environ,
        "HIVE_PORT": str(port),
        "HIVE_HOST": "127.0.0.1",
        "HIVE_DRAIN_SECONDS": "1",
    }
    proc = subprocess.Popen([sys.executable, "-m", "hive_mcp"], env=env)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(f"{base}/readyz").status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        time.sleep(0.3)
        assert httpx.get(f"{base}/readyz").status_code == 503
        assert proc.wait(timeout=15) in (0, -signal.SIGTERM)  # uvicorn re-raises the signal
    finally:
        if proc.poll() is None:
            proc.kill()
