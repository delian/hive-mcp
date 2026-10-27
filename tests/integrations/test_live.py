"""Integrations changed at run time: by an admin over MCP and on the admin page, no restart."""

from __future__ import annotations

import re
import time
from html import unescape
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import httpx
import pytest

from hive_mcp.domain.principal import Role
from hive_mcp.security.directory import DirectoryUser
from hive_mcp.slack.verify import sign
from tests.conformance.factory import ALICE, ROOT
from tests.integration.conftest import Live

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

SECRET = "slack-signing-secret-for-tests-1234"


@pytest.fixture
def ui(tmp_path: Path) -> Iterator[Live]:
    srv = Live(tmp_path / "data", admin_ui=True)
    srv.directory.upsert(
        DirectoryUser("ann@acme.com", "org1", Role.MEMBER, projects=frozenset({"p1"}))
    )
    srv.start()
    yield srv
    srv.stop()


def slack(live: Live, text: str, user: str = "UANN", channel: str = "C1") -> httpx.Response:
    form = {
        "team_id": "T9", "user_id": user, "channel_id": channel, "text": text,
        "trigger_id": f"t{time.time_ns()}",
    }  # fmt: skip
    body = urlencode(form).encode()
    stamp = str(int(time.time()))
    headers = {"x-slack-request-timestamp": stamp, "x-slack-signature": sign(SECRET, stamp, body)}
    return httpx.post(f"{live.url}/slack/commands", content=body, headers=headers)


def sign_in(live: Live, who) -> httpx.Client:
    client = httpx.Client(base_url=live.url, headers={"Origin": live.url}, follow_redirects=False)
    assert client.post("/admin/login", data={"token": live.token_for(who)}).status_code == 303
    return client


def csrf(client: httpx.Client, path: str) -> str:
    match = re.search(r"name=csrf value='([^']+)'", client.get(path).text)
    assert match
    return unescape(match.group(1))


async def test_an_admin_switches_slack_on_over_mcp_and_secrets_stay_out_of_chat(ui: Live):
    assert slack(ui, "help").status_code == 404  # not configured: the route says nothing
    async with ui.client(ui.token_for(ALICE)) as member:
        denied = await member.call_tool("kb_admin_integration", {"name": "slack"})
    assert denied.is_error and "admin" in denied.content[0].text
    async with ui.client(ui.token_for(ROOT)) as admin:
        shown = (
            await admin.call_tool("kb_admin_integration", {"name": "slack"})
        ).structured_content
        assert shown["enabled"] is False and shown["secrets_set"] == []
        early = await admin.call_tool(
            "kb_admin_integration_set", {"name": "slack", "enabled": True}
        )
        assert early.is_error and "workspace" in early.content[0].text  # incomplete: refused
        secret_try = await admin.call_tool(
            "kb_admin_integration_set", {"name": "slack", "signing_secret": SECRET}
        )
        assert secret_try.is_error  # secrets are not accepted through tool arguments
    # the secret goes in on the admin page (never through chat)
    client = sign_in(ui, ROOT)
    token = csrf(client, "/admin/integrations")
    saved = client.post(
        "/admin/integrations/save",
        data={"csrf": token, "name": "slack", "enabled": "1", "workspace": "T9",
              "secret_signing_secret": SECRET, "users": "UANN=ann@acme.com",
              "channel_projects": "C1=p1\n"},
    )  # fmt: skip
    assert saved.status_code == 303 and saved.headers["location"].endswith("done=saved")
    page = client.get("/admin/integrations").text
    assert (
        SECRET not in page and "(stored)" in page and "T9" in page
    )  # shown as set, never revealed
    reply = slack(ui, "save Handbook entry\ncontents for the team")
    assert (
        reply.status_code == 200 and "to project *p1*" in reply.json()["text"]
    )  # channel maps to p1
    async with ui.client(ui.token_for(ROOT)) as admin:
        out = (
            await admin.call_tool("kb_admin_integration_map", {"name": "slack", "channel": "C1"})
        ).structured_content
        assert out["channel_projects"] == {}  # unmapped through chat
        off = await admin.call_tool("kb_admin_integration_set", {"name": "slack", "enabled": False})
        assert off.structured_content["enabled"] is False
        audit = (
            await admin.call_tool("kb_admin_audit", {"action": "integration.update"})
        ).structured_content
    assert slack(ui, "help").status_code in (404,)  # off again, live
    assert len(audit["result"]) >= 3 and SECRET not in str(audit)


def test_the_pages_are_admin_only_and_forms_are_csrf_protected(ui: Live):
    member = sign_in(ui, ALICE)
    for path in ("/admin/integrations", "/admin/connectors", "/admin/stale", "/admin/duplicates"):
        assert member.get(path).status_code == 403
    admin = sign_in(ui, ROOT)
    for path in ("/admin/integrations", "/admin/connectors", "/admin/stale", "/admin/duplicates"):
        assert admin.get(path).status_code == 200, path
    no_csrf = admin.post("/admin/integrations/save", data={"name": "slack"})
    assert no_csrf.status_code in (400, 403)
    cross = httpx.Client(
        base_url=ui.url, cookies=admin.cookies, headers={"Origin": "https://evil.example"}
    )
    assert cross.post(
        "/admin/integrations/save", data={"name": "slack", "csrf": "x"}
    ).status_code in (400, 403)


def test_bad_form_input_is_an_error_page_not_a_crash(ui: Live):
    admin = sign_in(ui, ROOT)
    token = csrf(admin, "/admin/integrations")
    for data in (
        {"name": "slack", "enabled": "1"},  # incomplete
        {"name": "slack", "channel_projects": "no-equals-sign"},
        {"name": "nope"},
    ):
        response = admin.post("/admin/integrations/save", data={"csrf": token, **data})
        assert response.status_code in (400, 404) and "Not done" in response.text


def test_teams_page_shows_teams_fields_and_connectors_page_lists_nothing_by_default(ui: Live):
    admin = sign_in(ui, ROOT)
    page = admin.get("/admin/integrations").text
    assert (
        "Bot application id" in page and "/teams/messages" in page and "Slack workspace id" in page
    )
    assert "No connectors configured" in admin.get("/admin/connectors").text
    assert "Not edited or confirmed" in admin.get("/admin/stale?days=abc").text
    assert "No near-duplicates" in admin.get("/admin/duplicates").text


def test_token_form_accepts_a_per_token_rate_limit(ui: Live):
    admin = sign_in(ui, ROOT)
    token = csrf(admin, "/admin/tokens")
    base = {"csrf": token, "subject": "bot", "role": "member"}
    ok = admin.post("/admin/tokens/create", data={**base, "label": "fast", "rate": "600"})
    assert ok.status_code == 200 and "created" in ok.text
    assert "600/min" in admin.get("/admin/tokens").text
    bad = admin.post("/admin/tokens/create", data={**base, "label": "slow", "rate": "many"})
    assert bad.status_code == 400 and "whole number" in bad.text
