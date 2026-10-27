"""The Slack front door: request signatures, who is asking, and the two commands over real HTTP."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import httpx
import pytest

from hive_mcp.config import Settings
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.security.directory import DirectoryUser
from hive_mcp.slack.handlers import mrkdwn
from hive_mcp.slack.verify import WINDOW_SECONDS, sign, verify
from tests.conformance.factory import ROOT
from tests.integration.conftest import Live

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

SECRET, TEAM = "8f742231b10e8888abcd99yyyzzz85a5", "T0ACME"
USERS = {"UANN": "ann@acme.com", "UBOB": "bob@acme.com", "UGHOST": "ghost@acme.com"}


# ---------------------------------------------------------------- signatures
def test_signature_matches_slacks_documented_example():
    body = (
        b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow&channel_id=G8PSS9T3V"
        b"&channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner&command=%2Fwebhook-collect"
        b"&text=&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN"
        b"&trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c"
    )
    assert (
        sign("8f742231b10e8888abcd99yyyzzz85a5", "1531420618", body)
        == "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"
    )


def test_old_future_malformed_or_forged_requests_are_refused():
    now = 1_700_000_000
    body = b"a=1"
    good = sign(SECRET, str(now), body)
    assert verify(SECRET, str(now), good, body, lambda: now)
    assert verify(SECRET, str(now), good, body, lambda: now + WINDOW_SECONDS)
    assert not verify(SECRET, str(now), good, body, lambda: now + WINDOW_SECONDS + 1)
    assert not verify(SECRET, str(now), good, body, lambda: now - WINDOW_SECONDS - 1)
    assert not verify(SECRET, str(now), good, b"a=2", lambda: now)  # body changed
    assert not verify("other-secret", str(now), good, body, lambda: now)
    for ts, sig in ((None, good), (str(now), None), ("abc", good), ("", good), ("-5", good)):
        assert not verify(SECRET, ts, sig, body, lambda: now)


def test_mrkdwn_neutralises_mentions_and_links():
    assert (
        mrkdwn("<!channel> <http://evil|click> a&b")
        == "&lt;!channel&gt; &lt;http://evil|click&gt; a&amp;b"
    )


def test_settings_need_the_workspace_and_a_way_to_identify_people():
    with pytest.raises(ValueError, match="TEAM_ID"):
        Settings(slack_signing_secret=SECRET)
    with pytest.raises(ValueError, match="BOT_TOKEN or HIVE_SLACK_USERS"):
        Settings(slack_signing_secret=SECRET, slack_team_id=TEAM)
    assert Settings(slack_signing_secret=SECRET, slack_team_id=TEAM, slack_users=USERS)


# ---------------------------------------------------------------- over HTTP
@pytest.fixture
def slack(tmp_path: Path) -> Iterator[Live]:
    srv = Live(
        tmp_path / "data", slack_signing_secret=SECRET, slack_team_id=TEAM, slack_users=USERS
    )
    srv.directory.upsert(
        DirectoryUser("ann@acme.com", "org1", Role.MEMBER, projects=frozenset({"p1"}))
    )
    srv.directory.upsert(DirectoryUser("bob@acme.com", "org1", Role.MEMBER, disabled=True))
    srv.start()
    yield srv
    srv.stop()


def post(
    live: Live, path: str, fields: dict[str, str], *, ts: int | None = None, secret: str = SECRET
):
    body = urlencode(fields).encode()
    stamp = str(ts if ts is not None else int(time.time()))
    headers = {
        "x-slack-request-timestamp": stamp,
        "x-slack-signature": sign(secret, stamp, body),
        "content-type": "application/x-www-form-urlencoded",
    }
    return httpx.post(f"{live.url}{path}", content=body, headers=headers)


def command(live: Live, text: str, user: str = "UANN", team: str = TEAM) -> dict:
    form = {
        "team_id": team, "user_id": user, "command": "/hive", "text": text,
        "trigger_id": f"t{time.time_ns()}",  # Slack sends a fresh one with every request
    }  # fmt: skip
    response = post(live, "/slack/commands", form)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_routes_do_not_exist_unless_slack_is_configured(live: Live):
    assert httpx.post(f"{live.url}/slack/commands", content=b"x=1").status_code == 404


def test_unsigned_forged_stale_and_replayed_requests_are_refused(slack: Live):
    form = {"team_id": TEAM, "user_id": "UANN", "text": "help"}
    assert httpx.post(f"{slack.url}/slack/commands", data=form).status_code == 401
    assert post(slack, "/slack/commands", form, secret="wrong").status_code == 401
    assert post(slack, "/slack/commands", form, ts=int(time.time()) - 3600).status_code == 401
    body = urlencode(form).encode()
    stamp = str(int(time.time()))
    headers = {"x-slack-request-timestamp": stamp, "x-slack-signature": sign(SECRET, stamp, body)}
    first = httpx.post(f"{slack.url}/slack/commands", content=body, headers=headers)
    again = httpx.post(f"{slack.url}/slack/commands", content=body, headers=headers)
    assert (first.status_code, again.status_code) == (200, 401)  # a captured request is single use
    big = post(slack, "/slack/commands", {"text": "x" * 70_000})
    assert big.status_code == 401


def test_people_must_be_in_the_directory_and_in_the_right_workspace(slack: Live):
    assert "could not match" in command(slack, "help", user="UGHOST")["text"]  # not in directory
    assert "could not match" in command(slack, "help", user="UBOB")["text"]  # disabled
    assert "could not match" in command(slack, "help", user="UNKNOWN")["text"]  # not mapped
    assert "could not match" in command(slack, "help", team="T0OTHER")["text"]  # another workspace
    reply = command(slack, "help")
    assert reply["response_type"] == "ephemeral" and "/hive save" in reply["text"]


def test_save_then_search_and_everything_stays_private_to_the_asker(slack: Live):
    saved = command(slack, "save Deploy runbook\nrun make deploy then check /healthz")
    assert "Saved as a private note" in saved["text"] and saved["response_type"] == "ephemeral"
    again = command(slack, "save Deploy runbook\nrun make deploy then check /healthz")
    assert "Already saved" in again["text"]  # the same note twice is stored once
    found = command(slack, "how do we deploy")["text"]
    assert "Deploy runbook" in found and "your private notes" in found
    assert "Nothing found" in command(slack, "quantum pelican migration")["text"]
    assert "Nothing to save" in command(slack, "save   ")["text"]


def test_stored_text_cannot_inject_mentions_or_links_into_replies(slack: Live):
    command(slack, "save <!channel> <http://evil.example|free money> tips\nfor deploy checks")
    reply = command(slack, "deploy checks")["text"]
    assert "<!channel>" not in reply and "<http://evil" not in reply
    assert "&lt;!channel&gt;" in reply


def interaction(live: Live, payload: dict):
    return post(live, "/slack/interactions", {"payload": json.dumps(payload)})


def test_the_message_shortcut_saves_the_message_privately_and_is_idempotent(
    slack: Live, monkeypatch
):
    replies: list[str] = []

    async def capture(self, url, text):  # no network in tests
        replies.append(f"{url}|{text}")

    monkeypatch.setattr("hive_mcp.slack.handlers.SlackBot.reply", capture, raising=True)
    payload = {
        "type": "message_action", "callback_id": "hive_save", "response_url": "https://hooks.slack.com/x",
        "user": {"id": "UANN"}, "team": {"id": TEAM}, "channel": {"id": "C1", "name": "ops"},
        "message": {"text": "Rotate the API keys every 90 days &amp; log it", "ts": "1700000000.000100"},
    }  # fmt: skip
    assert interaction(slack, payload).status_code == 200
    assert interaction(slack, payload).status_code == 401  # identical request: replayed
    other = {**payload, "message": {**payload["message"], "ts": "1700000000.000200"}}
    assert interaction(slack, other).status_code == 200
    ignored = interaction(slack, {**payload, "callback_id": "something_else"})
    assert ignored.status_code == 200
    assert "Rotate the API keys" in command(slack, "API keys rotate")["text"]
    assert any("Saved as a private note" in r for r in replies)
    bad = post(slack, "/slack/interactions", {"payload": "{not json"})
    assert bad.status_code == 400
    wrong_user = interaction(
        slack, {**payload, "user": {"id": "UGHOST"}, "message": {"text": "x", "ts": "9"}}
    )
    assert wrong_user.status_code == 200 and any("could not match" in r for r in replies)


# ---------------------------------------------------------------- mentions in channels
def events(live: Live, payload: dict, secret: str = SECRET) -> httpx.Response:
    body = json.dumps(payload).encode()
    stamp = str(int(time.time()))
    headers = {
        "x-slack-request-timestamp": stamp, "x-slack-signature": sign(secret, stamp, body),
        "content-type": "application/json",
    }  # fmt: skip
    return httpx.post(f"{live.url}/slack/events", content=body, headers=headers)


@pytest.fixture
def mentions(tmp_path: Path, monkeypatch) -> Iterator[tuple[Live, list[tuple[str, str, str]]]]:
    posted: list[tuple[str, str, str]] = []

    async def capture(self, channel, thread, text):
        posted.append((channel, thread, text))

    monkeypatch.setattr("hive_mcp.slack.handlers.SlackBot.post", capture)
    srv = Live(
        tmp_path / "data", slack_signing_secret=SECRET, slack_team_id=TEAM, slack_users=USERS,
        slack_bot_token="xoxb-test-token",
    )  # fmt: skip
    srv.directory.upsert(
        DirectoryUser("ann@acme.com", "org1", Role.ADMIN, projects=frozenset({"p1"}))
    )
    srv.start()
    yield srv, posted
    srv.stop()


def mention(text: str, event_id: str, **kw) -> dict:
    event = {"type": "app_mention", "user": "UANN", "channel": "C1", "ts": "100.1", "text": text}
    return {
        "type": "event_callback",
        "team_id": TEAM,
        "event_id": event_id,
        "event": {**event, **kw},
    }


async def test_the_url_check_is_answered_and_mentions_are_off_until_an_admin_allows_them(mentions):
    live, posted = mentions
    check = events(live, {"type": "url_verification", "challenge": "abc123"})
    assert check.status_code == 200 and check.json() == {"challenge": "abc123"}
    assert events(live, {"type": "url_verification"}, secret="wrong").status_code == 401
    assert events(live, mention("<@UBOT> how do we deploy", "E1")).status_code == 200
    assert posted == []  # allow_mentions is false by default
    async with live.client(live.token_for(ROOT)) as admin:
        await admin.call_tool("kb_admin_integration_set", {"name": "slack", "allow_mentions": True})


async def test_a_mention_gets_a_thread_reply_with_only_what_the_channel_may_see(mentions):
    live, posted = mentions
    async with live.client(live.token_for(ROOT)) as admin:
        await admin.call_tool("kb_admin_integration_set", {"name": "slack", "allow_mentions": True})
        await admin.call_tool(
            "kb_admin_integration_map", {"name": "slack", "channel": "C1", "project": "p1"}
        )
    ann = Principal("ann@acme.com", "org1", Role.ADMIN, projects=frozenset({"p1"}))
    async with live.client(live.token_for(ann)) as writer:
        for title, body, extra in (
            ("Deploy runbook private", "private deploy steps nobody else sees", {}),
            (
                "Deploy runbook project",
                "project deploy steps for the team",
                {"share_with_project": "p1"},
            ),
        ):
            await writer.call_tool("kb_remember", {"title": title, "body": body, **extra})
    # make the mapping and flag visible to the server before asking (cache window is seconds)
    deadline = time.time() + 8
    while time.time() < deadline and not posted:
        events(live, mention("<@UBOT> deploy runbook steps", f"E{time.time_ns()}"))
        await asyncio.sleep(1)
    assert posted, "the bot never answered"
    channel, thread, text = posted[-1]
    assert (channel, thread) == ("C1", "100.1")  # a reply in the thread of the mention
    assert "Deploy runbook project" in text and "private" not in text.lower().replace(
        "your private notes", ""
    )
    count = len(posted)
    same = mention("<@UBOT> deploy runbook steps", "FIXED")
    events(live, same)
    events(live, same)  # a retry of the same event
    assert len(posted) - count <= 1
    n = len(posted)
    events(live, mention("loop", f"B{time.time_ns()}", bot_id="B1"))
    events(live, mention("loop", f"S{time.time_ns()}", subtype="message_changed"))
    events(live, {**mention("other", f"T{time.time_ns()}"), "team_id": "T0OTHER"})
    assert len(posted) == n  # bots, edits and other workspaces are ignored
