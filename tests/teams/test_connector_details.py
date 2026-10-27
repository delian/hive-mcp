"""The Bot Framework connector: the exact requests it makes, its token cache, its URL guard."""

from __future__ import annotations

import json

import httpx
import pytest

from hive_mcp.security.oauth.upstream import UpstreamError
from hive_mcp.teams.connector import (
    SCOPE,
    BotConnector,
    _conversation_url,
    check_service_url,
)

SERVICE = "https://smba.trafficmanager.net/emea/"


def connector(responses: list[httpx.Response] | None = None, expires_in=3600):
    sent: list[httpx.Request] = []
    queue = list(responses or [])

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.host == "login.test":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": expires_in})
        return queue.pop(0) if queue else httpx.Response(200, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BotConnector("app", "pw", "tenant", http=http, token_url="https://login.test/t"), sent


def test_the_url_guard_accepts_teams_hosts_and_returns_them_without_a_trailing_slash():
    assert check_service_url(SERVICE) == "https://smba.trafficmanager.net/emea"
    assert check_service_url("https://x.botframework.com//") == "https://x.botframework.com"
    assert check_service_url("https://x.botframework.us/a") == "https://x.botframework.us/a"
    for bad in (
        "http://smba.trafficmanager.net/", "https://evil.example/", "https://a.trafficmanager.net/",
        "https://user@smba.trafficmanager.net/", "https://botframework.com.evil.example/",
        "https://evilbotframework.com/", "", "smba.trafficmanager.net",
    ):  # fmt: skip
        with pytest.raises(UpstreamError, match=r"^refusing a service url outside the Bot"):
            check_service_url(bad)
    assert check_service_url("https://botframework.com".replace("//", "//a.")) != ""


def test_conversation_urls_escape_every_part_including_slashes():
    url = _conversation_url("https://h", "19:a/b@thread", "activities", "x/../y")
    assert url == "https://h/v3/conversations/19%3Aa%2Fb%40thread/activities/x%2F..%2Fy"


async def test_a_reply_asks_for_a_token_once_then_posts_plain_text():
    c, sent = connector()
    await c.reply(SERVICE, "19:c", "act1", "hello", now=0.0)
    token_request, post = sent
    form = dict(x.split("=") for x in token_request.content.decode().split("&"))
    assert form["grant_type"] == "client_credentials" and form["client_id"] == "app"
    assert (
        form["client_secret"] == "pw"
        and form["scope"].replace("%3A", ":").replace("%2F", "/") == SCOPE
    )
    assert (
        str(post.url)
        == "https://smba.trafficmanager.net/emea/v3/conversations/19%3Ac/activities/act1"
    )
    assert post.headers["authorization"] == "Bearer tok"
    assert json.loads(post.content) == {"type": "message", "text": "hello", "textFormat": "plain"}
    await c.reply(SERVICE, "19:c", "act2", "again", now=100.0)
    assert len(sent) == 3  # the token was kept
    assert sent[2].headers["authorization"] == "Bearer tok"  # the kept token is the one sent


async def test_the_token_is_renewed_a_minute_before_it_expires():
    c, sent = connector(expires_in=1000)
    await c.reply(SERVICE, "c", "a", "x", now=0.0)
    await c.reply(SERVICE, "c", "a", "x", now=939.0)  # 61 s left: reuse
    assert len([r for r in sent if r.url.host == "login.test"]) == 1
    await c.reply(SERVICE, "c", "a", "x", now=940.0)  # exactly 60 s left: renew
    assert len([r for r in sent if r.url.host == "login.test"]) == 2
    await c.reply(SERVICE, "c", "a", "x", now=2000.0)  # long gone
    assert len([r for r in sent if r.url.host == "login.test"]) == 3


async def test_a_token_answer_without_a_lifetime_lasts_an_hour():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.host)
        if request.url.host == "login.test":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, json={})

    c = BotConnector(
        "a",
        "p",
        "t",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        token_url="https://login.test/t",
    )
    await c.reply(SERVICE, "c", "a", "x", now=0.0)
    await c.reply(SERVICE, "c", "a", "x", now=3500.0)
    assert sent.count("login.test") == 1
    await c.reply(SERVICE, "c", "a", "x", now=3541.0)
    assert sent.count("login.test") == 2


async def test_token_failures_become_one_clear_error():
    def broken(body: httpx.Response):
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: body))
        return BotConnector("a", "p", "t", http=http, token_url="https://login.test/t")

    for response in (
        httpx.Response(401, text="no"), httpx.Response(200, text="not json"),
        httpx.Response(200, json={"nope": 1}),
    ):  # fmt: skip
        with pytest.raises(UpstreamError) as caught:
            await broken(response).reply(SERVICE, "c", "a", "x", now=0.0)
        assert str(caught.value) == "could not get a Bot Framework token"


async def test_the_default_token_url_names_the_tenant():
    c = BotConnector("a", "p", "my-tenant")
    assert c._token_url == "https://login.microsoftonline.com/my-tenant/oauth2/v2.0/token"


async def test_send_posts_to_the_conversation_and_reports_failures():
    c, sent = connector()
    await c.send(SERVICE, "19:c", "ping", now=0.0)
    assert (
        str(sent[-1].url)
        == "https://smba.trafficmanager.net/emea/v3/conversations/19%3Ac/activities"
    )
    assert json.loads(sent[-1].content)["text"] == "ping"
    failing, _ = connector([httpx.Response(500)])
    with pytest.raises(UpstreamError) as caught:
        await failing.send(SERVICE, "c", "ping", now=0.0)
    assert str(caught.value) == "could not send the Teams message"
    bad_reply, _ = connector([httpx.Response(200)])
    with pytest.raises(UpstreamError):
        await bad_reply.send("https://evil.example/", "c", "x", now=0.0)


async def test_reply_failures_are_reported_and_need_a_valid_service_url():
    def exploding(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.test":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        raise httpx.ConnectError("down")

    http = httpx.AsyncClient(transport=httpx.MockTransport(exploding))
    c = BotConnector("a", "p", "t", http=http, token_url="https://login.test/t")
    with pytest.raises(UpstreamError) as caught:
        await c.reply(SERVICE, "c", "a", "x", now=0.0)
    assert str(caught.value) == "could not send the Teams reply"
    with pytest.raises(UpstreamError, match="refusing a service url"):
        await c.reply("https://evil.example/", "c", "a", "x", now=0.0)


async def test_member_email_prefers_email_then_the_principal_name_and_never_raises():
    cases = [
        ({"email": "a@acme.com", "userPrincipalName": "upn@acme.com"}, "a@acme.com"),
        ({"userPrincipalName": "upn@acme.com"}, "upn@acme.com"),
        ({"email": 5}, None),
        ({}, None),
    ]
    for body, expected in cases:
        c, sent = connector([httpx.Response(200, json=body)])
        assert await c.member_email(SERVICE, "19:c", "29:u", now=0.0) == expected
    assert (
        str(sent[-1].url)
        == "https://smba.trafficmanager.net/emea/v3/conversations/19%3Ac/members/29%3Au"
    )
    assert sent[-1].headers["authorization"] == "Bearer tok"
    c, _ = connector([httpx.Response(200, text="not json")])
    assert await c.member_email(SERVICE, "c", "u", now=0.0) is None

    def down(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.test":
            return httpx.Response(200, json={"access_token": "t"})
        raise httpx.ConnectError("x")

    http = httpx.AsyncClient(transport=httpx.MockTransport(down))
    c = BotConnector("a", "p", "t", http=http, token_url="https://login.test/t")
    assert await c.member_email(SERVICE, "c", "u", now=0.0) is None
    with pytest.raises(UpstreamError):
        await c.member_email("http://evil.example", "c", "u", now=0.0)


async def test_send_carries_the_token_and_the_plain_message_body():
    c, sent = connector()
    await c.send(SERVICE, "19:c", "ping", now=0.0)
    request = sent[-1]
    assert request.headers["authorization"] == "Bearer tok"
    assert json.loads(request.content) == {"type": "message", "text": "ping", "textFormat": "plain"}


async def test_the_default_lifetime_is_exactly_an_hour_and_the_token_itself_is_sent():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.host == "login.test":
            return httpx.Response(200, json={"access_token": "the-token"})
        return httpx.Response(200, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c = BotConnector("a", "p", "t", http=http, token_url="https://login.test/t")
    await c.reply(SERVICE, "c", "a", "x", now=0.0)
    assert sent[-1].headers["authorization"] == "Bearer the-token"
    await c.reply(SERVICE, "c", "a", "x", now=3540.5)  # 59.5 s left of 3600: already renewed
    assert [r.url.host for r in sent].count("login.test") == 2


def test_only_slashes_are_trimmed_from_a_service_url():
    assert check_service_url("https://x.botframework.com/aX") == "https://x.botframework.com/aX"
    assert check_service_url("https://x.botframework.com/aX/") == "https://x.botframework.com/aX"
