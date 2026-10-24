"""Runs the real ASGI app (real storage in a temp dir, real bearer tokens) under uvicorn."""

from __future__ import annotations

import socket
import threading
import time
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from hive_mcp.app import create_app
from hive_mcp.config import Settings
from hive_mcp.ops.drain import DrainingServer
from hive_mcp.ops.state import Lifecycle
from hive_mcp.security.tokens import TokenStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from hive_mcp.domain.principal import Principal


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Live:
    def __init__(self, data_dir: Path, grace: float = 0.0) -> None:
        self.port = free_port()
        self.lifecycle = Lifecycle()
        self.settings = Settings(
            host="127.0.0.1",
            port=self.port,
            data_dir=data_dir,
            public_url=f"http://127.0.0.1:{self.port}",
        )
        self.tokens = TokenStore(self.settings.tokens_path)
        config = uvicorn.Config(
            create_app(self.settings, self.lifecycle),
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
        for _ in range(200):
            if self.server.started and self.lifecycle.ready:
                return
            time.sleep(0.05)
        raise RuntimeError("server did not become ready")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)

    def token_for(self, principal: Principal, label: str | None = None, **kw: Any) -> str:
        return self.tokens.create(
            principal, label=label or f"t-{principal.id}-{time.time_ns()}", **kw
        )

    def client(self, token: str | None, mode: str = "auto") -> Client:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        http = httpx2.AsyncClient(headers=headers, timeout=30)
        return Client(streamable_http_client(f"{self.url}/mcp", http_client=http), mode=mode)


@pytest.fixture
def live(tmp_path: Path) -> Iterator[Live]:
    srv = Live(tmp_path / "data")
    srv.start()
    yield srv
    srv.stop()
