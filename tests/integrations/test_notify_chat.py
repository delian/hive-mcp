"""Slack and Teams direct messages for notifications: only when switched on, skipping the unknown."""

import json

import httpx
import pytest

from hive_mcp.domain.errors import NotifyError
from hive_mcp.integrations.config import IntegrationConfig
from hive_mcp.notify.chat import SlackDmNotifier, TeamsDmNotifier
from hive_mcp.ports.notify import Notification
from hive_mcp.teams.connector import BotConnector

NOTE = Notification(
    "promotion.requested",
    "Promotion request: <Plan>",
    "ann asks to share it",
    ("ann@acme.com", "bob@acme.com"),
)


def config(name="slack", enabled=True, **secrets):
    return IntegrationConfig(name, enabled, "T1", "app-1", secrets=secrets)


def getter(cfg):
    async def get():
        return cfg

    return get


class Slack:
    def __init__(self, users, post_ok=True):
        self.users, self.post_ok, self.calls = users, post_ok, []

    def client(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            assert request.headers["authorization"] == "Bearer xoxb-1"
            if "lookupByEmail" in request.url.path:
                email = request.url.params["email"]
                if email in self.users:
                    return httpx.Response(200, json={"ok": True, "user": {"id": self.users[email]}})
                return httpx.Response(200, json={"ok": False, "error": "users_not_found"})
            return httpx.Response(
                200,
                json={"ok": self.post_ok, "error": None if self.post_ok else "channel_not_found"},
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_slack_dms_go_to_found_people_with_escaped_text_and_lookups_are_cached(stack):
    slack = Slack({"ann@acme.com": "U1"})
    n = SlackDmNotifier(
        getter(config(bot_token="xoxb-1")), lambda: stack.backend.kv, slack.client()
    )
    await n.send(NOTE)
    posts = [json.loads(r.content) for r in slack.calls if "postMessage" in r.url.path]
    assert [p["channel"] for p in posts] == ["U1"]  # bob was not found: skipped, no error
    assert "&lt;Plan&gt;" in posts[0]["text"] and "<Plan>" not in posts[0]["text"]
    await n.send(NOTE)
    assert sum("lookupByEmail" in r.url.path for r in slack.calls) == 2  # ann and bob, once each


async def test_slack_is_skipped_when_off_or_without_a_token_and_fails_loudly_when_slack_does(stack):
    slack = Slack({"ann@acme.com": "U1"})
    for cfg in (config(enabled=False, bot_token="xoxb-1"), config()):
        await SlackDmNotifier(getter(cfg), lambda: stack.backend.kv, slack.client()).send(NOTE)
    assert slack.calls == []
    refusing = Slack({"ann@acme.com": "U1"}, post_ok=False)
    n = SlackDmNotifier(
        getter(config(bot_token="xoxb-1")), lambda: stack.backend.kv, refusing.client()
    )
    with pytest.raises(NotifyError, match="refused the message"):
        await n.send(NOTE)
    down = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(NotifyError, match="unreachable"):
        await SlackDmNotifier(
            getter(config(bot_token="xoxb-1")), lambda: stack.backend.kv, down
        ).send(Notification("e", "s", "t", ("zed@acme.com",)))


async def test_teams_dms_use_the_remembered_chat_and_skip_people_who_never_wrote_to_the_bot(stack):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2" in request.url.path:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        sent.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await stack.backend.kv.put(
        "teams:ref",
        "ann@acme.com",
        {"serviceUrl": "https://smba.trafficmanager.net/emea/", "conversation": "a:1abc"},
    )
    cfg = config("teams", app_password="pw")
    n = TeamsDmNotifier(
        getter(cfg),
        lambda: stack.backend.kv,
        lambda c: BotConnector(c.app_id, "pw", c.workspace, http=http),
    )
    await n.send(NOTE)
    assert len(sent) == 1  # bob has no remembered chat
    url, body = sent[0]
    assert url == "https://smba.trafficmanager.net/emea/v3/conversations/a%3A1abc/activities"
    assert body["textFormat"] == "plain" and "Promotion request" in body["text"]
    for off in (config("teams", enabled=False, app_password="pw"), config("teams")):
        await TeamsDmNotifier(getter(off), lambda: stack.backend.kv).send(NOTE)
    assert len(sent) == 1


async def test_a_failing_teams_delivery_is_an_error_so_it_is_retried(stack):
    await stack.backend.kv.put(
        "teams:ref", "ann@acme.com", {"serviceUrl": "https://evil.example/", "conversation": "c"}
    )  # an address outside the Bot Framework is refused
    cfg = config("teams", app_password="pw")
    n = TeamsDmNotifier(getter(cfg), lambda: stack.backend.kv)
    with pytest.raises(NotifyError):
        await n.send(Notification("e", "s", "t", ("ann@acme.com",)))
