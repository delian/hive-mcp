"""End-to-end over real HTTP: bearer auth, tool wiring, caller isolation, error messages."""

import time

import httpx

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


async def call(client, name, **args):
    return await client.call_tool(name, args)


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
