"""A real server with the background thread on: it serves, runs its jobs elsewhere, stops cleanly."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

from tests.conformance.factory import ALICE
from tests.integration.conftest import Live
from tests.integration.test_mcp_e2e import call

if TYPE_CHECKING:
    from pathlib import Path


def background_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "hive-background" and t.is_alive()]


async def test_the_server_works_with_the_runner_in_its_own_thread(tmp_path: Path) -> None:
    server = Live(tmp_path / "data", background_thread=True)
    server.start()
    try:
        assert len(background_threads()) == 1
        async with server.client(server.token_for(ALICE)) as alice:
            saved = (
                await call(alice, "kb_remember", title="Pulse", body="radar echo timing")
            ).structured_content
            found = (await call(alice, "kb_consult", query="radar echo")).structured_content
            assert [r["id"] for r in found["results"]] == [saved["id"]]
    finally:
        server.stop()
    for thread in background_threads():
        await asyncio.to_thread(thread.join, 10)
    assert background_threads() == []  # the thread ended with the server
