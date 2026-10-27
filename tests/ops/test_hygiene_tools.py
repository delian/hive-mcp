"""kb_duplicates (and, later, the stale review tools) over HTTP."""

import pytest

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


async def test_project_digest_tool_and_pinned_resource(live: Live):
    async with live.client(live.token_for(ALICE)) as alice:
        await alice.call_tool(
            "kb_remember",
            {"title": "Dwell time plan", "body": "Use 12 ms.", "share_with_project": "p1"},
        )
        tool = (await alice.call_tool("kb_digest", {"project": "p1"})).structured_content
        both = await alice.call_tool("kb_digest", {"project": "p1", "team": "t1"})
        elsewhere = await alice.call_tool("kb_digest", {"project": "p-nope"})
        resource = await alice.read_resource("kb://project/p1/digest")
        with pytest.raises(Exception, match="Error creating resource"):
            await alice.read_resource("kb://project/p-nope/digest")
    assert [i["title"] for i in tool["items"]] == ["Dwell time plan"]
    assert both.is_error and "not both" in both.content[0].text
    assert elsewhere.is_error and "not a member" in elsewhere.content[0].text
    assert "Dwell time plan" in resource.contents[0].text


async def test_admin_activity_tool_and_page(live: Live):
    from hive_mcp.ops.usage import METER
    from tests.conformance.factory import ROOT

    async with live.client(live.token_for(ALICE)) as alice:
        await alice.call_tool("kb_remember", {"title": "A note", "body": "words"})
        denied = await alice.call_tool("kb_admin_activity", {})
    assert denied.is_error and "admin" in denied.content[0].text
    async with live.client(live.token_for(ROOT)) as admin:
        # the server's own meter is flushed on a timer; flush this process's counts directly
        out = (await admin.call_tool("kb_admin_activity", {"days": 7})).structured_content
        bad = await admin.call_tool("kb_admin_activity", {"days": 0})
    assert out["days"] == 7 and [p["user"] for p in out["people"]] == ["alice"]
    assert out["people"][0]["writes"] == 1 and out["people"][0]["items"] == 1
    assert bad.is_error
    assert METER._counts[("org1", "alice")]["kb_remember"] >= 1  # every tool call is counted
