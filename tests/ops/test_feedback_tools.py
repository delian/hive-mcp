"""kb_consult and kb_get feed the search-feedback trail, only when it is enabled."""

import pytest

from tests.conformance.factory import ALICE, ROOT
from tests.integration.conftest import Live


@pytest.fixture
def recording(tmp_path):
    srv = Live(tmp_path / "data", search_feedback=True)
    srv.start()
    yield srv
    srv.stop()


async def trail(live: Live, action: str) -> list[dict]:
    async with live.client(live.token_for(ROOT)) as admin:
        result = await admin.call_tool("kb_admin_audit", {"action": action})
    return result.structured_content["result"]


async def test_consult_and_get_are_recorded_when_enabled(recording: Live):
    async with recording.client(recording.token_for(ALICE)) as alice:
        made = (
            await alice.call_tool(
                "kb_remember", {"title": "Dwell time plan", "body": "Use 12 ms dwell for the scan."}
            )
        ).structured_content
        hits = (await alice.call_tool("kb_consult", {"query": "dwell time"})).structured_content
        await alice.call_tool("kb_get", {"id": made["id"]})
    assert [r["id"] for r in hits["results"]] == [made["id"]]
    (query,) = await trail(recording, "search.query")
    assert query["actor"] == "alice" and query["details"] == {"q": "dwell time", "hits": made["id"]}
    (opened,) = await trail(recording, "search.open")
    assert opened["target"] == made["id"]


async def test_nothing_is_recorded_by_default(live: Live):
    async with live.client(live.token_for(ALICE)) as alice:
        made = (
            await alice.call_tool("kb_remember", {"title": "Plan", "body": "Some plan text."})
        ).structured_content
        await alice.call_tool("kb_consult", {"query": "plan"})
        await alice.call_tool("kb_get", {"id": made["id"]})
    assert await trail(live, "search.query") == [] and await trail(live, "search.open") == []
