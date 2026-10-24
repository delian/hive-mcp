"""The authenticated endpoint must serve 2025-era (legacy) and 2026-era (modern) clients alike."""

import pytest

from tests.conformance.factory import ALICE
from tests.integration.conftest import Live

ERAS = [("legacy", "2025-11-25"), ("auto", "2026-07-28")]


@pytest.mark.parametrize(("mode", "expected"), ERAS)
async def test_both_protocol_eras_can_remember_and_consult(
    live: Live, mode: str, expected: str
) -> None:
    async with live.client(live.token_for(ALICE), mode=mode) as client:
        assert client.protocol_version == expected
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"kb_consult", "kb_remember", "kb_get", "kb_update", "kb_share"} <= names
        saved = await client.call_tool(
            "kb_remember", {"title": f"Era {mode}", "body": "pulse timing notes"}
        )
        assert not saved.is_error and saved.structured_content["created"] is True
        found = await client.call_tool("kb_consult", {"query": "pulse timing"})
        assert [r["title"] for r in found.structured_content["results"]] == [f"Era {mode}"]
