"""The same endpoint must work for 2025-era (legacy) and 2026-era (modern) clients."""

import pytest
from mcp import Client

from tests.integration.conftest import Live

ERAS = [("legacy", "2025-11-25"), ("auto", "2026-07-28")]


@pytest.mark.parametrize(("mode", "expected"), ERAS)
async def test_ping_tool_both_protocol_eras(live: Live, mode: str, expected: str) -> None:
    async with Client(f"{live.url}/mcp", mode=mode) as client:
        assert client.protocol_version == expected
        tools = await client.list_tools()
        assert "ping" in {t.name for t in tools.tools}
        result = await client.call_tool("ping", {"message": "hi"})
        assert result.content[0].text == "hi"  # type: ignore[union-attr]
