"""kb_duplicates (and, later, the stale review tools) over HTTP."""

from tests.conformance.factory import ALICE
from tests.integration.conftest import Live

TEXT = "the radar range equation relates power and distance through the antenna gain"


async def test_duplicates_over_http(live: Live):
    async with live.client(live.token_for(ALICE)) as alice:
        ids = []
        for title in ("Radar range", "Radar range copy"):
            made = await alice.call_tool("kb_remember", {"title": title, "body": TEXT})
            ids.append(made.structured_content["id"])
        out = (await alice.call_tool("kb_duplicates", {})).structured_content
        bad = await alice.call_tool("kb_duplicates", {"threshold": 0.1})
    assert [sorted(g["ids"]) for g in out["groups"]] == [sorted(ids)]
    assert out["scanned"] == 2 and out["next_cursor"] is None
    assert bad.is_error and "threshold" in bad.content[0].text


async def test_stale_review_and_confirm_over_http(live: Live):
    async with live.client(live.token_for(ALICE)) as alice:
        made = await alice.call_tool("kb_remember", {"title": "Fresh", "body": "just written"})
        nid = made.structured_content["id"]
        assert (await alice.call_tool("kb_review_stale", {})).structured_content["items"] == []
        bad = await alice.call_tool("kb_review_stale", {"days": 1})
        ok = await alice.call_tool("kb_confirm", {"id": nid})
        missing = await alice.call_tool("kb_confirm", {"id": "nope"})
    assert bad.is_error and "days must be" in bad.content[0].text
    assert ok.structured_content["id"] == nid and ok.structured_content["confirmed_at"]
    assert missing.is_error
