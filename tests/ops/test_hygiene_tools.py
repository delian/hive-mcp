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


async def test_merge_proposal_needs_a_model_and_conflicts_work_without_one(live: Live):
    async with live.client(live.token_for(ALICE)) as alice:
        a = (
            await alice.call_tool(
                "kb_remember",
                {
                    "title": "Dwell",
                    "body": "the radar dwell time is 12 ms for the scan",
                    "share_with_project": "p1",
                },
            )
        ).structured_content
        b = (
            await alice.call_tool(
                "kb_remember",
                {
                    "title": "Dwell",
                    "body": "the radar dwell time is 20 ms for the scan",
                    "share_with_project": "p1",
                },
            )
        ).structured_content
        proposal = await alice.call_tool("kb_merge_propose", {"ids": [a["id"], b["id"]]})
        found = (await alice.call_tool("kb_conflicts", {"id": a["id"]})).structured_content[
            "result"
        ]
        bad = await alice.call_tool("kb_conflicts", {"id": a["id"], "limit": 0})
    assert proposal.is_error and "no language model" in proposal.content[0].text
    assert [c["id"] for c in found] == [b["id"]] and "different numbers" in found[0]["reasons"][0]
    assert found[0]["model_says_contradiction"] is None
    assert bad.is_error


async def test_rank_signals_do_not_turn_ratings_into_votes(tmp_path):
    srv = Live(tmp_path / "data", rank_signals=True)
    srv.start()
    try:
        async with srv.client(srv.token_for(ALICE)) as alice:
            made = (
                await alice.call_tool("kb_remember", {"title": "Plan", "body": "Some plan text."})
            ).structured_content
            ok = await alice.call_tool("kb_rate", {"id": made["id"], "useful": True})
        assert ok.structured_content["recorded"] is False  # no search feedback configured
    finally:
        srv.stop()


async def test_a_session_trail_lists_what_the_work_relied_on_and_links_the_session_note(live: Live):
    async with live.client(live.token_for(ALICE)) as alice:
        used = (
            await alice.call_tool(
                "kb_remember",
                {"title": "Deploy runbook", "body": "run make deploy then check healthz"},
            )
        ).structured_content
        other = (
            await alice.call_tool(
                "kb_remember", {"title": "Deploy history", "body": "deploys happened on mondays"}
            )
        ).structured_content
        found = (
            await alice.call_tool("kb_consult", {"query": "deploy", "session": "sess-0001"})
        ).structured_content
        await alice.call_tool("kb_get", {"id": used["id"], "session": "sess-0001"})
        trail = (
            await alice.call_tool("kb_session_sources", {"session": "sess-0001"})
        ).structured_content["result"]
        report = (
            await alice.call_tool(
                "kb_report_session", {"summary": "deployed it", "session": "sess-0001"}
            )
        ).structured_content
        graph = await alice.call_tool("kb_graph", {"id": report["id"]})
        bad = await alice.call_tool("kb_session_sources", {"session": "x"})
        empty = (
            await alice.call_tool("kb_session_sources", {"session": "never-used"})
        ).structured_content["result"]
    assert {r["id"] for r in found["results"]} == {used["id"], other["id"]}
    assert (trail[0]["id"], trail[0]["opened"]) == (used["id"], True)  # opened first
    assert {s["id"]: s["opened"] for s in trail} == {used["id"]: True, other["id"]: False}
    assert used["id"] in str(graph.structured_content) and other["id"] not in str(
        graph.structured_content
    )
    assert bad.is_error and "session labels" in bad.content[0].text and empty == []


async def test_another_persons_session_label_shows_nothing(live: Live):
    from tests.conformance.factory import BOB

    async with live.client(live.token_for(ALICE)) as alice:
        made = (
            await alice.call_tool("kb_remember", {"title": "Mine", "body": "x"})
        ).structured_content
        await alice.call_tool("kb_get", {"id": made["id"], "session": "shared-label"})
    async with live.client(live.token_for(BOB)) as bob:
        assert (
            await bob.call_tool("kb_session_sources", {"session": "shared-label"})
        ).structured_content["result"] == []
