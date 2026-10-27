"""The Teams front door: token verification, the connector's URL guard, bot behaviour, the route."""

from __future__ import annotations

import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from starlette.applications import Starlette
from starlette.testclient import TestClient

from hive_mcp.domain.principal import Role
from hive_mcp.integrations.config import IntegrationConfig
from hive_mcp.security.directory import DirectoryUser, UserDirectory
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.security.oauth.upstream import UpstreamError
from hive_mcp.services.models import RememberRequest
from hive_mcp.teams.connector import BotConnector, check_service_url
from hive_mcp.teams.handlers import TeamsBot, clean_text
from hive_mcp.teams.routes import TeamsRoutes
from hive_mcp.teams.verify import ISSUER, BotFrameworkVerifier

APP, TENANT = "11111111-aaaa-bbbb-cccc-222222222222", "tenant-1"
SERVICE = "https://smba.trafficmanager.net/emea/"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def bot_token(key=KEY, **claims) -> str:
    base = {"iss": ISSUER, "aud": APP, "exp": int(time.time()) + 600, "serviceurl": SERVICE}
    return jwt.encode({**base, **claims}, key, algorithm="RS256", headers={"kid": "k1"})


def keys_client(calls=None) -> httpx.AsyncClient:
    jwk = {**RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True), "kid": "k1"}

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if request.url.path.endswith("openidconfiguration"):
            return httpx.Response(200, json={"jwks_uri": "https://login.test/keys"})
        return httpx.Response(200, json={"keys": [jwk]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- token verification
async def test_a_valid_token_for_this_bot_and_url_is_accepted_and_keys_are_cached():
    calls: list[str] = []
    v = BotFrameworkVerifier(APP, http=keys_client(calls))
    await v.verify(f"Bearer {bot_token()}", SERVICE)
    await v.verify(
        f"bearer {bot_token()}", SERVICE.rstrip("/")
    )  # trailing slash is not a difference
    assert len(calls) == 2  # config + keys once


@pytest.mark.parametrize(
    "header",
    [
        None, "", "Basic abc", "Bearer ", "Bearer not.a.jwt",
        f"Bearer {bot_token(aud='someone-else')}",
        f"Bearer {bot_token(iss='https://evil.example')}",
        f"Bearer {bot_token(exp=int(time.time()) - 3600)}",
        f"Bearer {bot_token(serviceurl='https://evil.example/')}",
        f"Bearer {bot_token(rsa.generate_private_key(public_exponent=65537, key_size=2048))}",
    ],
)  # fmt: skip
async def test_forged_or_mismatched_tokens_are_refused(header):
    with pytest.raises(UpstreamError):
        await BotFrameworkVerifier(APP, http=keys_client()).verify(header, SERVICE)


async def test_an_unsigned_or_hs256_token_cannot_pick_its_own_algorithm():
    none_token = jwt.encode(
        {"iss": ISSUER, "aud": APP, "exp": int(time.time()) + 60, "serviceurl": SERVICE},
        key="", algorithm="none", headers={"kid": "k1"},
    )  # fmt: skip
    hs = jwt.encode({"iss": ISSUER, "aud": APP, "exp": int(time.time()) + 60}, "x" * 40, "HS256")
    for token in (none_token, hs):
        with pytest.raises(UpstreamError):
            await BotFrameworkVerifier(APP, http=keys_client()).verify(f"Bearer {token}", SERVICE)


async def test_an_unreachable_key_service_is_an_error_not_a_pass():
    down = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(UpstreamError):
        await BotFrameworkVerifier(APP, http=down).verify(f"Bearer {bot_token()}", SERVICE)


# ---------------------------------------------------------------- connector
def test_replies_only_go_to_the_bot_framework():
    assert (
        check_service_url("https://smba.trafficmanager.net/emea/")
        == "https://smba.trafficmanager.net/emea"
    )
    assert check_service_url("https://smba.botframework.com") == "https://smba.botframework.com"
    for bad in (
        "http://smba.trafficmanager.net/",
        "https://evil.example/",
        "https://trafficmanager.net.evil.com/",
        "https://user:pw@smba.trafficmanager.net/",
        "ftp://x.botframework.com",
        "",
        "smba.botframework.com",
    ):
        with pytest.raises(UpstreamError):
            check_service_url(bad)


async def test_the_connector_fetches_a_token_once_and_sends_plain_text():
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if "oauth2" in request.url.path:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if "members" in request.url.path:
            return httpx.Response(200, json={"email": "Ann@Acme.com"})
        return httpx.Response(200, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c = BotConnector(APP, "secret", TENANT, http=http)
    await c.reply(SERVICE, "19:abc@thread", "act/1", "hello", now=0)
    await c.reply(SERVICE, "19:abc@thread", "act/2", "again", now=1)
    assert sum("oauth2" in r.url.path for r in sent) == 1
    post = next(r for r in sent if "activities" in r.url.path)
    assert post.headers["authorization"] == "Bearer tok" and "act%2F1" in str(post.url)
    assert json.loads(post.content) == {"type": "message", "text": "hello", "textFormat": "plain"}
    assert TENANT in str(next(r for r in sent if "oauth2" in r.url.path).url)
    assert await c.member_email(SERVICE, "19:abc@thread", "29:u", now=2) == "Ann@Acme.com"
    with pytest.raises(UpstreamError):
        await c.reply("https://evil.example/", "c", "a", "x", now=3)
    bad = BotConnector(
        APP,
        "s",
        TENANT,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401))),
    )
    with pytest.raises(UpstreamError):
        await bad.reply(SERVICE, "c", "a", "x", now=0)


# ---------------------------------------------------------------- bot behaviour
class Outbox:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def reply(self, service_url, conversation, activity, text, now) -> None:
        self.sent.append(text)

    async def member_email(self, service_url, conversation, user_id, now):
        return {
            "29:ann": "ann@acme.com",
            "29:ghost": "ghost@acme.com",
            "29:bob": "bob@acme.com",
        }.get(user_id)


@pytest.fixture
def world(stack, tmp_path):
    directory = UserDirectory(AtomicJsonFile(tmp_path / "u.json", default=[]))
    directory.upsert(DirectoryUser("ann@acme.com", "org1", Role.MEMBER, projects=frozenset({"p1"})))
    outbox = Outbox()

    def make(**cfg) -> TeamsBot:
        config = IntegrationConfig("teams", True, TENANT, APP, secrets={"app_password": "x"}, **cfg)
        return TeamsBot(config, outbox, directory, lambda: stack, lambda: stack.backend.kv)  # type: ignore[arg-type]

    return make, outbox, stack


def msg(  # noqa: PLR0913
    text, *, conv="personal", aad="aad-ann", uid="29:ann", tenant=TENANT, cid="19:c1", mid="m1"
):
    return {
        "type": "message", "id": mid, "text": text, "serviceUrl": SERVICE,
        "from": {"id": uid, "aadObjectId": aad},
        "conversation": {"id": cid, "conversationType": conv},
        "channelData": {"tenant": {"id": tenant}},
    }  # fmt: skip


def test_message_text_is_cleaned_of_mentions_and_markup():
    assert (
        clean_text("<at>Hive</at> how do we deploy&nbsp;&amp; ship?")
        == "how do we deploy\xa0& ship?"
    )
    assert clean_text("<at>Hive</at>  <b>save</b> this") == "save this"


async def test_a_personal_chat_gets_help_save_and_search(world):
    make, outbox, _ = world
    bot = make()
    await bot.handle(msg("help"))
    assert "shared memory" in outbox.sent[-1]
    await bot.handle(msg("save Runbook\nrestart the ingest worker"))
    assert outbox.sent[-1].startswith("Saved as a private note: Runbook")
    await bot.handle(msg("save Runbook\nrestart the ingest worker"))
    assert outbox.sent[-1].startswith("Already saved")
    await bot.handle(msg("how to restart the ingest worker"))
    assert "Runbook" in outbox.sent[-1] and "your private notes" in outbox.sent[-1]
    await bot.handle(msg("quantum pelican migration"))
    assert outbox.sent[-1].startswith("Nothing found")
    await bot.handle(msg("save   "))
    assert "Nothing to save" in outbox.sent[-1]


async def test_strangers_other_tenants_and_other_activity_types_get_nothing_or_no_data(world):
    make, outbox, _ = world
    bot = make()
    await bot.handle(msg("help", aad="aad-ghost", uid="29:ghost"))  # not in the directory
    assert "could not match" in outbox.sent[-1]
    await bot.handle(msg("help", aad="aad-x", uid="29:nobody"))  # no email at all
    assert "could not match" in outbox.sent[-1]
    n = len(outbox.sent)
    await bot.handle(msg("help", tenant="other-tenant"))
    await bot.handle({**msg("help"), "type": "conversationUpdate"})
    await bot.handle({**msg(""), "text": ""})
    assert len(outbox.sent) == n


async def test_channels_are_ignored_until_allowed_and_then_quote_only_what_everyone_may_see(world):
    make, outbox, stack = world
    ann = DirectoryUser("ann@acme.com", "org1", Role.MEMBER, projects=frozenset({"p1"})).principal()
    from hive_mcp.domain.scope import Scope, ScopeLevel

    for title, scope in (
        ("Deploy private", None),
        ("Deploy project p1", Scope(ScopeLevel.PROJECT, "p1")),
    ):
        await stack.knowledge.remember(
            ann, RememberRequest(title=title, body="deploy steps for the team", scope=scope)
        )
    channel = msg("<at>Hive</at> deploy steps", conv="channel")
    await make().handle(channel)
    assert outbox.sent == []  # channel use is off by default
    await make(allow_mentions=True).handle(channel)
    assert (
        "Nothing found" in outbox.sent[-1]
        or "Nothing to share" in outbox.sent[-1]
        or "only quote" in outbox.sent[-1]
    )
    assert "Deploy private" not in outbox.sent[-1] and "Deploy project" not in outbox.sent[-1]
    mapped = make(allow_mentions=True, channel_projects={"19:c1": "p1"})
    await mapped.handle(channel)
    assert "Deploy project p1" in outbox.sent[-1] and "Deploy private" not in outbox.sent[-1]
    await mapped.handle(msg("save Channel note\nshared decision", conv="channel", mid="m9"))
    assert outbox.sent[-1] == "Saved. Shared with project p1."  # nothing of the note is echoed
    await make(allow_mentions=True).handle(
        msg("save Quiet note\nprivate thing", conv="channel", mid="m10")
    )
    assert outbox.sent[-1] == "Saved. (private to you)" and "Quiet" not in outbox.sent[-1]


# ---------------------------------------------------------------- the route
class FakeBot:
    def __init__(self) -> None:
        self.seen: list[dict] = []

    async def handle(self, activity) -> None:
        self.seen.append(activity)


def routes(stack, enabled=True, bot=None):
    bot = bot or FakeBot()
    config = IntegrationConfig("teams", enabled, TENANT, APP, secrets={"app_password": "x"})
    verifier = BotFrameworkVerifier(APP, http=keys_client())

    async def get_config():
        return config

    app = Starlette(
        routes=TeamsRoutes(get_config, lambda c: (verifier, bot), lambda: stack.backend.kv).routes()
    )
    return TestClient(app), bot


def post(client, activity, token=None, raw=None):
    headers = {"Authorization": f"Bearer {token or bot_token()}"}
    return client.post("/teams/messages", content=raw or json.dumps(activity), headers=headers)


def test_the_route_verifies_dedupes_and_limits(stack):
    client, bot = routes(stack)
    activity = msg("hello")
    assert post(client, activity).status_code == 200 and len(bot.seen) == 1
    assert post(client, activity).status_code == 200 and len(bot.seen) == 1  # redelivery: once
    assert post(client, {**activity, "id": "m2"}, token="junk").status_code == 401
    assert (
        post(client, {**activity, "id": "m3", "serviceUrl": "https://evil.example/"}).status_code
        == 401
    )
    assert (
        client.post("/teams/messages", content=json.dumps(activity)).status_code == 401
    )  # no token
    assert post(client, None, raw="not json").status_code == 400
    assert post(client, None, raw="[1,2]").status_code == 400
    assert post(client, None, raw=b"x" * 300_000).status_code == 413
    assert len(bot.seen) == 1


def test_a_disabled_integration_does_not_exist(stack):
    client, bot = routes(stack, enabled=False)
    assert post(client, msg("hello")).status_code == 404 and bot.seen == []
