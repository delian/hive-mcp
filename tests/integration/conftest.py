"""Fixture that runs the real ASGI app under uvicorn on an ephemeral port."""

from __future__ import annotations

import socket
import threading
import time
from typing import TYPE_CHECKING

import pytest
import uvicorn

from hive_mcp.app import create_app
from hive_mcp.config import Settings
from hive_mcp.ops.drain import DrainingServer
from hive_mcp.ops.state import Lifecycle

if TYPE_CHECKING:
    from collections.abc import Iterator


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Live:
    def __init__(self, grace: float = 0.0) -> None:
        self.port = free_port()
        self.lifecycle = Lifecycle()
        settings = Settings(host="127.0.0.1", port=self.port)
        config = uvicorn.Config(
            create_app(settings, self.lifecycle),
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
        )
        self.server = DrainingServer(config, self.lifecycle, grace)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("server did not start")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def live() -> Iterator[Live]:
    srv = Live()
    srv.start()
    yield srv
    srv.stop()
