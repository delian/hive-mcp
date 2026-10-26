"""End-to-end over real HTTP: bearer auth, tool wiring, caller isolation, error messages."""

import base64
import time

import httpx
import pytest

from hive_mcp.domain.principal import Principal, Role
from tests.conformance.factory import ALICE, BOB, CAROL, ROOT, VIVI, ZOE
from tests.integration.conftest import Live

ACCEPT = {"Accept": "application/json, text/event-stream"}
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def post(live: Live, token: str | None, **kw):
    headers = {**ACCEPT, **({"Authorization": f"Bearer {token}"} if token else {})}
    return httpx.post(
        f"{live.url}/mcp", json=LIST, headers={**headers, **kw.pop("headers", {})}, **kw
    )


async def call(client, tool, /, **args):
    return await client.call_tool(tool, args)


# ---------------------------------------------------------------- authentication
def test_missing_or_invalid_tokens_get_401_with_discovery_link(live: Live) -> None:
    for token in (None, "hive_not-a-token", "sk-" + "a" * 40):
        r = post(live, token)
        assert r.status_code == 401
        assert "resource_metadata=" in r.headers["www-authenticate"]
    assert "/.well-known/oauth-protected-resource" in post(live, None).headers["www-authenticate"]


def test_valid_token_is_accepted_and_revocation_is_immediate(live: Live) -> None:
    token = live.token_for(ALICE, label="temp")
    assert post(live, token).status_code == 200
    assert live.tokens.revoke("temp")
    assert post(live, token).status_code == 401  # no restart needed


def test_expired_token_is_rejected(live: Live) -> None:
    token = live.token_for(ALICE, label="short", expires_in_days=0.00001)
    time.sleep(1.2)
    assert post(live, token).status_code == 401


def test_dns_rebinding_protection_rejects_foreign_host_and_origin(live: Live) -> None:
    token = live.token_for(ALICE)
    assert post(live, token, headers={"Host": "evil.example.com"}).status_code in (400, 403, 421)
    assert post(live, token, headers={"Origin": "https://evil.example.com"}).status_code == 403
    assert post(live, token, headers={"Origin": live.url}).status_code == 200


# ---------------------------------------------------------------- tools
async def test_tools_describe_themselves_and_mark_reads_read_only(live: Live) -> None:
    async with live.client(live.token_for(ALICE)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for name in ("kb_consult", "kb_get", "kb_get_log"):
            assert tools[name].annotations.read_only_hint is True
        for name in ("kb_remember", "kb_update", "kb_share", "kb_report_session"):
            assert tools[name].annotations.read_only_hint is False
        assert "BEFORE starting a task" in tools["kb_consult"].description
        assert "user_id" not in str(
            tools["kb_remember"].input_schema
        )  # identity is never an argument
        assert (await client.list_prompts()).prompts


async def test_callers_only_see_their_own_knowledge_and_cannot_spoof_identity(live: Live) -> None:
    async with live.client(live.token_for(ALICE)) as alice, live.client(live.token_for(BOB)) as bob:
        saved = await call(
            alice, "kb_remember", title="Alice private", body="narwhal telemetry notes"
        )
        item_id = saved.structured_content["id"]
        assert (await call(alice, "kb_consult", query="narwhal")).structured_content["results"]
        assert (await call(bob, "kb_consult", query="narwhal")).structured_content["results"] == []
        hidden = await call(bob, "kb_get", id=item_id)
        assert hidden.is_error and "not found, or not visible" in hidden.content[0].text
        # Extra arguments naming another user are ignored: identity comes from the token only.
        spoof = await call(
            bob, "kb_consult", query="narwhal", user="alice", principal="alice", org="acme"
        )
        assert not spoof.is_error and spoof.structured_content["results"] == []


async def test_tenants_are_isolated(live: Live) -> None:
    async with live.client(live.token_for(ALICE)) as alice, live.client(live.token_for(ZOE)) as zoe:
        await call(
            alice,
            "kb_remember",
            title="Org one",
            body="shared secret sauce",
            share_with_project="p1",
        )
        assert (
            await call(zoe, "kb_consult", query="sauce", other_groups="always")
        ).structured_content == {
            "results": [],
            "from_other_groups": [],
            "notes": [],
        }


async def test_sharing_with_a_project_and_external_labelling(live: Live) -> None:
    async with (
        live.client(live.token_for(ALICE)) as alice,
        live.client(live.token_for(BOB)) as bob,
        live.client(live.token_for(CAROL)) as carol,
        live.client(live.token_for(ROOT)) as root,
    ):
        mine = await call(
            alice,
            "kb_remember",
            title="Waveform plan",
            body="chirp bandwidth plan",
            tags=["Domain/Radar"],
        )
        shared = await call(
            alice, "kb_share", id=mine.structured_content["id"], expected_revision=1, project="p1"
        )
        assert (
            shared.structured_content["scope"] == "project:p1"
            and shared.structured_content["revision"] == 2
        )
        bobs = (await call(bob, "kb_consult", query="chirp bandwidth")).structured_content
        assert [r["origin"] for r in bobs["results"]] == ["project p1"] and bobs["results"][0][
            "tags"
        ] == ["domain/radar"]
        assert (await call(carol, "kb_consult", query="chirp bandwidth")).structured_content[
            "results"
        ] == []
        # Knowledge shared inside project p2 never reaches Alice, even when requested.
        await call(
            root,
            "kb_remember",
            title="Antenna table",
            body="x band antenna gain",
            share_with_project="p2",
        )
        for policy in ("auto", "always"):
            seen = (
                await call(alice, "kb_consult", query="antenna gain", other_groups=policy)
            ).structured_content
            assert seen["results"] == [] and seen["from_other_groups"] == []


async def test_error_messages_are_actionable_and_hide_internals(live: Live) -> None:
    async with (
        live.client(live.token_for(ALICE)) as alice,
        live.client(live.token_for(VIVI)) as viewer,
    ):
        saved = (
            await call(alice, "kb_remember", title="Versioned", body="first body")
        ).structured_content
        ok = await call(alice, "kb_update", id=saved["id"], expected_revision=1, body="second body")
        assert ok.structured_content["revision"] == 2
        stale = await call(
            alice, "kb_update", id=saved["id"], expected_revision=1, body="lost update"
        )
        assert (
            stale.is_error
            and "kb_get" in stale.content[0].text
            and "revision" in stale.content[0].text
        )
        denied = await call(viewer, "kb_remember", title="Nope", body="viewers are read-only")
        assert denied.is_error and "cannot create" in denied.content[0].text
        wide = await call(alice, "kb_remember", title="Org wide", body="x", share_with_project="p2")
        assert wide.is_error  # not a member of p2
        bad_kind = await call(alice, "kb_remember", title="T", body="b", kind="poem")
        assert bad_kind.is_error and "use one of" in bad_kind.content[0].text
        bad_limit = await call(alice, "kb_consult", query="x", limit=500)
        assert bad_limit.is_error and "between 1 and 25" in bad_limit.content[0].text
        for text in (stale.content[0].text, denied.content[0].text):
            assert "Traceback" not in text and "sqlite" not in text.lower() and 'File "' not in text


async def test_secrets_are_masked_and_reported_through_the_tool(live: Live) -> None:
    secret = "ghp_" + "Zy9Xw8Vu7T" * 4
    async with live.client(live.token_for(ALICE)) as alice:
        saved = (
            await call(alice, "kb_remember", title="Deploy", body=f"use {secret} to deploy")
        ).structured_content
        assert saved["redactions"] == {"github-token": 1} and "masked" in saved["message"]
        item = (await call(alice, "kb_get", id=saved["id"])).structured_content
        assert secret not in item["body"] and "[REDACTED:github-token]" in item["body"]


async def test_session_report_log_roundtrip_and_privacy(live: Live) -> None:
    async with live.client(live.token_for(ALICE)) as alice, live.client(live.token_for(BOB)) as bob:
        report = await call(
            alice,
            "kb_report_session",
            summary="Calibrated the receiver\nDetails follow.",
            log="step 1\nstep 2 done",
            outcomes=["gain set"],
            project="radar-x",
            idempotency_key="s1",
        )
        assert report.structured_content["created"] is True
        again = await call(
            alice,
            "kb_report_session",
            summary="Calibrated the receiver\nDetails follow.",
            log="step 1\nstep 2 done",
            outcomes=["gain set"],
            project="radar-x",
            idempotency_key="s1",
        )
        assert (
            again.structured_content["created"] is False
            and again.structured_content["id"] == report.structured_content["id"]
        )
        sid = report.structured_content["id"]
        assert (await call(alice, "kb_get_log", id=sid)).structured_content[
            "log"
        ] == "step 1\nstep 2 done"
        assert (await call(alice, "kb_get", id=sid)).structured_content["has_log"] is True
        assert (await call(bob, "kb_get_log", id=sid)).is_error
        compact = await call(alice, "kb_report_compaction", summary="Condensed context")
        assert not compact.is_error


async def test_large_bodies_are_truncated_on_read(live: Live) -> None:
    async with live.client(live.token_for(ALICE)) as alice:
        saved = (
            await call(alice, "kb_remember", title="Big", body="word " * 8000)
        ).structured_content
        item = (await call(alice, "kb_get", id=saved["id"])).structured_content
        assert item["truncated"] is True and len(item["body"]) == 20_000


async def test_promotion_workflow_over_mcp(live: Live) -> None:
    team = Principal("tina", "org1", Role.MEMBER, teams=frozenset({"t1"}))
    async with (
        live.client(live.token_for(ALICE)) as alice,
        live.client(live.token_for(team)) as tina,
        live.client(live.token_for(ROOT)) as root,
    ):
        item = (
            await call(alice, "kb_remember", title="Dwell", body="dwell time plan")
        ).structured_content
        denied = await call(alice, "kb_share", id=item["id"], expected_revision=1, project="p9")
        assert denied.is_error  # not a member of p9
        req = (
            await call(
                alice, "kb_request_promotion", id=item["id"], to="team", owner="t1", reason="r"
            )
        ).structured_content
        assert req["status"] == "pending" and req["target"] == "team:t1"
        assert req["review_body"] is None  # requesters never get the review view
        queue = (await call(root, "kb_list_promotions")).structured_content["result"]
        assert [q["review_body"] for q in queue] == ["dwell time plan"]
        assert (await call(alice, "kb_decide_promotion", id=req["id"], decision="approve")).is_error
        assert not (await call(tina, "kb_consult", query="dwell time")).structured_content[
            "results"
        ]
        done = await call(root, "kb_decide_promotion", id=req["id"], decision="approve", note="ok")
        assert done.structured_content["status"] == "approved"
        found = (await call(tina, "kb_consult", query="dwell time")).structured_content["results"]
        assert [r["id"] for r in found] == [item["id"]]
        mine = (await call(alice, "kb_list_promotions", status="all")).structured_content["result"]
        assert [m["status"] for m in mine] == ["approved"]


async def test_graph_tools_over_mcp(live: Live) -> None:
    async with live.client(live.token_for(ALICE)) as alice, live.client(live.token_for(BOB)) as bob:
        a = (
            await call(alice, "kb_remember", title="Finding", body="finding text")
        ).structured_content
        b = (
            await call(alice, "kb_remember", title="Source", body="source text")
        ).structured_content
        linked = await call(
            alice, "kb_link", from_id=a["id"], to_id=b["id"], relation="derived_from"
        )
        assert linked.structured_content["created"] is True
        graph = (await call(alice, "kb_graph", id=a["id"])).structured_content
        assert [n["title"] for n in graph["nodes"]] == ["Finding", "Source"]
        assert graph["edges"] == [
            {"from_id": a["id"], "to_id": b["id"], "relation": "derived_from"}
        ]
        assert (await call(bob, "kb_graph", id=a["id"])).is_error  # private to alice
        assert (await call(bob, "kb_link", from_id=a["id"], to_id=b["id"])).is_error
        assert (await call(alice, "kb_graph", id=a["id"], depth=9)).is_error
        gone = await call(
            alice, "kb_unlink", from_id=a["id"], to_id=b["id"], relation="derived_from"
        )
        assert not gone.is_error


async def test_ingest_tool_over_mcp(live: Live) -> None:
    async with live.client(live.token_for(ALICE)) as alice:
        page = b"<html><title>Waveforms</title><body><h1>Chirps</h1><p>Linear chirp bandwidth.</p></body></html>"
        done = (
            await call(
                alice,
                "kb_ingest",
                name="w.html",
                content=base64.b64encode(page).decode(),
                encoding="base64",
            )
        ).structured_content
        assert done["parts"] == 1 and done["new_parts"] == 1
        found = (await call(alice, "kb_consult", query="linear chirp bandwidth")).structured_content
        assert [r["id"] for r in found["results"]] == done["ids"]
        again = (
            await call(
                alice,
                "kb_ingest",
                name="w.html",
                content=base64.b64encode(page).decode(),
                encoding="base64",
            )
        ).structured_content
        assert again["new_parts"] == 0
        assert (await call(alice, "kb_ingest", name="x.bin", content="abc")).is_error
        assert (
            await call(alice, "kb_ingest", name="x.pdf", content="!!!", encoding="base64")
        ).is_error


async def test_curation_tools_over_mcp(live: Live) -> None:
    async with live.client(live.token_for(ALICE)) as alice:
        ids = []
        for i in range(2):
            r = await call(
                alice,
                "kb_remember",
                title=f"Pulse {i}",
                body="pulse repetition frequency notes",
                tags=["Old"],
            )
            ids.append(r.structured_content["id"])
        dry = (await call(alice, "kb_rename_tag", old="old", new="new")).structured_content
        assert len(dry["changed"]) == 2  # dry run by default
        assert (await call(alice, "kb_get", id=ids[0])).structured_content["tags"] == ["old"]
        applied = (
            await call(alice, "kb_rename_tag", old="old", new="new", dry_run=False)
        ).structured_content
        assert sorted(applied["changed"]) == sorted(ids)
        retag = (
            await call(alice, "kb_retag", ids=ids, add_tags=["x"], remove_tags=["new"])
        ).structured_content
        assert len(retag["changed"]) == 2
        sim = (await call(alice, "kb_similar", id=ids[0])).structured_content["result"]
        assert [s["id"] for s in sim] == [ids[1]] and sim[0]["score"] > 0.5
        merged = (
            await call(alice, "kb_merge", ids=ids, title="Pulse", body="consolidated")
        ).structured_content
        assert merged["created"] and sorted(merged["retired"]) == sorted(ids)
        assert (await call(alice, "kb_get", id=ids[0])).structured_content["status"] == "superseded"
        assert (await call(alice, "kb_merge", ids=ids[:1], title="x", body="y")).is_error


async def test_prompts_are_listed_and_filled_in(live: Live) -> None:
    async with live.client(live.token_for(ALICE)) as client:
        prompts = {p.name: p for p in (await client.list_prompts()).prompts}
        assert {
            "consult_before_task",
            "end_of_session_report",
            "before_compaction",
            "tidy_tag",
            "review_promotions",
        } <= set(prompts)
        assert [a.name for a in prompts["consult_before_task"].arguments] == ["task"]
        got = await client.get_prompt("consult_before_task", {"task": "tune the chirp"})
        text = got.messages[0].content.text
        assert "tune the chirp" in text and "kb_consult" in text and "EXTERNAL" in text
        assert (
            "kb_report_session"
            in (await client.get_prompt("end_of_session_report")).messages[0].content.text
        )
        assert (
            'tags=["radar"]'
            in (await client.get_prompt("tidy_tag", {"tag": "radar"})).messages[0].content.text
        )


async def test_resources_show_items_with_origin_and_respect_access(live: Live) -> None:
    async with (
        live.client(live.token_for(ALICE)) as alice,
        live.client(live.token_for(CAROL)) as carol,
    ):
        saved = (
            await call(
                alice, "kb_remember", title="Chirp plan", body="use a 20 MHz chirp", tags=["rf"]
            )
        ).structured_content
        uri = f"kb://item/{saved['id']}"
        text = (await alice.read_resource(uri)).contents[0].text
        assert (
            text.startswith("# Chirp plan")
            and "your private notes" in text
            and "use a 20 MHz chirp" in text
        )
        assert "rf" in text and f"id: {saved['id']}" in text
        with pytest.raises(Exception, match="Error creating resource"):  # no hint whether it exists
            await carol.read_resource(uri)  # private to alice
        recent = (await alice.read_resource("kb://recent")).contents[0].text
        assert f"[Chirp plan]({uri})" in recent
        assert "Chirp plan" not in (await carol.read_resource("kb://recent")).contents[0].text
        assert "kb_consult" in (await alice.read_resource("kb://guide")).contents[0].text
        templates = [
            t.uri_template for t in (await alice.list_resource_templates()).resource_templates
        ]
        assert "kb://item/{item_id}" in templates
        assert {str(r.uri) for r in (await alice.list_resources()).resources} >= {
            "kb://guide",
            "kb://recent",
        }


def test_prompts_and_resources_need_authentication(live: Live) -> None:
    for method in ("prompts/list", "resources/list"):
        r = httpx.post(
            f"{live.url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method}, headers=ACCEPT
        )
        assert r.status_code == 401


async def test_browse_and_tag_tools(live: Live) -> None:
    async with live.client(live.token_for(ALICE)) as alice, live.client(live.token_for(BOB)) as bob:
        for i in range(5):
            await call(
                alice,
                "kb_remember",
                title=f"Note {i}",
                body="words go here",
                tags=["domain/rf", f"n/{i % 2}"],
                kind="formula" if i == 0 else "note",
            )
        seen, cursor = [], None
        while True:
            page = (await call(alice, "kb_list", limit=2, cursor=cursor)).structured_content
            seen += [i["title"] for i in page["items"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert seen == [f"Note {i}" for i in (4, 3, 2, 1, 0)]  # newest first, no repeats or gaps
        formulas = (await call(alice, "kb_list", kinds=["formula"])).structured_content["items"]
        assert [i["title"] for i in formulas] == ["Note 0"]
        both = (await call(alice, "kb_list", tags=["domain/rf", "n/1"])).structured_content["items"]
        assert sorted(i["title"] for i in both) == ["Note 1", "Note 3"]
        tags = (await call(alice, "kb_tags")).structured_content["result"]
        assert tags[0] == {"tag": "domain/rf", "items": 5}
        assert [
            t["tag"]
            for t in (await call(alice, "kb_tags", prefix="n/")).structured_content["result"]
        ] == ["n/0", "n/1"]
        assert (await call(bob, "kb_tags")).structured_content[
            "result"
        ] == []  # alice's notes are private
        assert (await call(bob, "kb_list")).structured_content["items"] == []
        assert (await call(alice, "kb_list", limit=0)).is_error
        assert (await call(alice, "kb_list", cursor="garbage")).is_error
        assert (await call(alice, "kb_tags", limit=9999)).is_error


async def test_purge_tool_over_mcp(live: Live) -> None:
    async with live.client(live.token_for(ALICE)) as alice, live.client(live.token_for(BOB)) as bob:
        mine = (
            await call(alice, "kb_remember", title="Doomed", body="to be erased")
        ).structured_content
        shared = (
            await call(alice, "kb_remember", title="Shared", body="in p1", share_with_project="p1")
        ).structured_content
        assert (
            await call(alice, "kb_purge", id=mine["id"], confirm="wrong")
        ).is_error  # must confirm
        assert (
            await call(bob, "kb_purge", id=shared["id"], confirm=shared["id"])
        ).is_error  # not his
        assert (
            await call(bob, "kb_purge", id=mine["id"], confirm=mine["id"])
        ).is_error  # cannot even see it
        gone = await call(alice, "kb_purge", id=mine["id"], confirm=mine["id"])
        assert gone.structured_content["title"] == "Doomed"
        assert (await call(alice, "kb_get", id=mine["id"])).is_error
        assert (await call(alice, "kb_consult", query="erased")).structured_content["results"] == []
        assert (
            await call(alice, "kb_purge", id=mine["id"], confirm=mine["id"])
        ).is_error  # already gone
        tools = {t.name: t for t in (await alice.list_tools()).tools}
        assert tools["kb_purge"].annotations.destructive_hint is True


async def test_admin_tools_over_mcp(live: Live) -> None:
    async with (
        live.client(live.token_for(ALICE)) as alice,
        live.client(live.token_for(ROOT)) as root,
    ):
        await call(alice, "kb_remember", title="One", body="some text")
        await call(alice, "kb_remember", title="Two", body="more text")
        assert (await call(alice, "kb_admin_usage")).is_error  # members cannot
        assert (await call(alice, "kb_admin_audit")).is_error
        usage = (await call(root, "kb_admin_usage")).structured_content["result"]
        assert usage == [
            {
                "user": "alice",
                "items": 2,
                "characters": 24,
                "last_activity": usage[0]["last_activity"],
            }
        ]
        events = (
            await call(root, "kb_admin_audit", actor="alice", action="item.create")
        ).structured_content["result"]
        assert [e["action"] for e in events] == ["item.create", "item.create"] and events[0][
            "seq"
        ] < events[1]["seq"]
        assert "some text" not in str(events)  # ids only, never content
        assert (await call(root, "kb_admin_audit", limit=0)).is_error


async def test_consult_returns_links_and_conflict_notes(live: Live) -> None:
    async with live.client(live.token_for(ALICE)) as alice:
        a = (
            await call(alice, "kb_remember", title="Claim A", body="pelican arrival is in spring")
        ).structured_content
        b = (
            await call(alice, "kb_remember", title="Claim B", body="pelican arrival is in autumn")
        ).structured_content
        await call(alice, "kb_link", from_id=a["id"], to_id=b["id"], relation="contradicts")
        res = (await call(alice, "kb_consult", query="pelican arrival")).structured_content
        top = next(r for r in res["results"] if r["id"] == a["id"])
        assert top["links"] == [
            {
                "relation": "contradicts",
                "direction": "out",
                "id": b["id"],
                "title": "Claim B",
                "external": False,
            }
        ]
        assert any("contradict" in n for n in res["notes"])
        plain = (
            await call(alice, "kb_consult", query="pelican arrival", include_links=False)
        ).structured_content
        assert all(r["links"] == [] for r in plain["results"]) and not any(
            "contradict" in n for n in plain["notes"]
        )


async def test_entity_tools_over_mcp(live: Live) -> None:
    async with live.client(live.token_for(ALICE)) as alice:
        saved = (
            await call(
                alice, "kb_entity_save", name="Pulse Doppler Radar", type="system", aliases=["PDR"]
            )
        ).structured_content
        assert saved["created"] and saved["entity"]["aliases"] == ["pdr", "pulse-doppler-radar"]
        again = (
            await call(alice, "kb_entity_save", name="pdr", type="system", aliases=["PD radar"])
        ).structured_content
        assert (
            not again["created"]
            and again["entity"]["id"] == saved["entity"]["id"]
            and again["added_aliases"] == ["pd-radar"]
        )
        found = (
            await call(alice, "kb_entity_resolve", name="Pulse-Doppler radar")
        ).structured_content["result"]
        assert [(f["id"], f["match"]) for f in found] == [(saved["entity"]["id"], "exact")]
        note = (
            await call(alice, "kb_remember", title="Flight test", body="Flew it today.")
        ).structured_content
        mentioned = (
            await call(alice, "kb_entity_mention", id=note["id"], entities=["PDR", "nonsense"])
        ).structured_content
        assert mentioned == {"PDR": "linked", "nonsense": "not found"}
        graph = (
            await call(alice, "kb_graph", id=saved["entity"]["id"], relations=["mentions"])
        ).structured_content
        assert {n["title"] for n in graph["nodes"]} == {"Pulse Doppler Radar", "Flight test"}
        assert (await call(alice, "kb_entity_save", name="!!!", type="system")).is_error


async def test_history_tools_over_mcp(live: Live) -> None:
    async with live.client(live.token_for(ALICE)) as alice, live.client(live.token_for(BOB)) as bob:
        item = (
            await call(alice, "kb_remember", title="Plan", body="first idea\nsecond idea")
        ).structured_content
        await call(
            alice, "kb_update", id=item["id"], expected_revision=1, body="first idea\nbetter idea"
        )
        hist = (await call(alice, "kb_history", id=item["id"])).structured_content["result"]
        assert [(h["revision"], h["current"]) for h in hist] == [(2, True), (1, False)]
        assert hist[1]["replaced_by"] == "alice" and hist[1]["scope"] == "private:alice"
        old = (await call(alice, "kb_version", id=item["id"], revision=1)).structured_content
        assert old["body"] == "first idea\nsecond idea" and old["current"] is False
        diff = (await call(alice, "kb_diff", id=item["id"], from_revision=1)).structured_content[
            "result"
        ]
        assert "-second idea" in diff and "+better idea" in diff
        restored = (
            await call(alice, "kb_restore", id=item["id"], revision=1, expected_revision=2)
        ).structured_content
        assert restored["revision"] == 3
        assert (
            (await call(alice, "kb_get", id=item["id"]))
            .structured_content["body"]
            .endswith("second idea")
        )
        assert (
            await call(alice, "kb_restore", id=item["id"], revision=1, expected_revision=3)
        ).is_error  # no-op
        assert (await call(bob, "kb_history", id=item["id"])).is_error  # private to alice
        assert (await call(alice, "kb_version", id=item["id"], revision=77)).is_error
