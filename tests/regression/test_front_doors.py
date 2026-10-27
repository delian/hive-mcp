"""Regression tests for defects found in the chat front doors (Slack, Teams, integrations)."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from hive_mcp.config import Settings
from hive_mcp.domain.principal import Role
from hive_mcp.integrations.config import IntegrationConfig
from hive_mcp.integrations.store import env_defaults
from hive_mcp.security.directory import DirectoryUser, UserDirectory
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.slack.handlers import SlackBot
from hive_mcp.slack.identity import SlackIdentity

TEAM = "T0ACME"
ANN = DirectoryUser("ann@acme.com", "org1", Role.MEMBER, projects=frozenset({"p1"}))


@pytest.fixture
def slack_world(stack, tmp_path):
    directory = UserDirectory(AtomicJsonFile(tmp_path / "u.json", default=[]))
    directory.upsert(ANN)

    def make(channel_projects=None, http=None, token=None, pinned=None) -> SlackBot:
        identity = SlackIdentity(
            directory,
            lambda: stack.backend.kv,
            team_id=TEAM,
            bot_token=token,
            pinned=pinned if pinned is not None else {"UANN": "ann@acme.com"},
            http=http,
        )
        return SlackBot(lambda: stack, identity, channel_projects=channel_projects)

    return make, stack


def slash(text, channel="C1"):
    return {"team_id": TEAM, "user_id": "UANN", "channel_id": channel, "text": text}


async def test_slash_save_keeps_different_notes_from_one_channel(slack_world):
    """BUG-FD-1: every `/hive save` in a channel used one idempotency key (team, channel, "cmd"),
    so the second, different note was refused with an idempotency conflict instead of saved."""
    make, _ = slack_world
    bot = make()
    first = await bot.command(slash("save Deploy runbook\nrun make deploy"))
    second = await bot.command(slash("save Rotate keys\nevery 90 days"))
    assert "Saved" in first["text"]
    assert "Saved" in second["text"], second["text"]
    again = await bot.command(slash("save Rotate keys\nevery 90 days"))
    assert "Already saved" in again["text"]


async def test_message_shortcut_saves_into_the_channels_project(slack_world):
    """BUG-FD-2: "Save to Hive" ignored the channel to project mapping (docs/SLACK.md: it saves into
    the project for members, like `/hive save`), so the note stayed private."""
    make, _ = slack_world
    bot = make(channel_projects={"C1": "p1"})
    payload = {
        "user": {"id": "UANN"}, "team": {"id": TEAM}, "channel": {"id": "C1", "name": "ops"},
        "message": {"text": "Rotate the API keys", "ts": "1700000000.000100"},
    }  # fmt: skip
    assert "to project *p1*" in await bot.shortcut(payload)
    elsewhere = {**payload, "channel": {"id": "C9", "name": "x"}}
    assert "private note" in await bot.shortcut(elsewhere)


def test_a_non_ascii_signature_is_refused_not_a_crash():
    """BUG-FD-3: hmac.compare_digest on str raised TypeError for a non-ASCII signature header,
    so a forged request got a 500 instead of a 401."""
    from hive_mcp.slack.verify import verify

    assert verify("secret", "1700000000", "v0=é", b"a=1", lambda: 1_700_000_000) is False


class TtlSpy:
    """A key-value store that remembers the lifetime asked for on each put."""

    def __init__(self, inner):
        self.inner, self.ttls = inner, []

    async def put(self, namespace, key, value, *, ttl_seconds=None):
        self.ttls.append(ttl_seconds)
        await self.inner.put(namespace, key, value, ttl_seconds=ttl_seconds)

    def __getattr__(self, name):
        return getattr(self.inner, name)


async def test_a_failed_email_lookup_is_not_remembered_for_an_hour(stack, tmp_path):
    """BUG-FD-4: a Slack outage during one users.info call stored "no email" for an hour, so the
    person stayed "not matched" long after Slack recovered (same for the Teams member lookup)."""
    directory = UserDirectory(AtomicJsonFile(tmp_path / "u.json", default=[]))
    directory.upsert(ANN)
    spy = TtlSpy(stack.backend.kv)
    down = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    identity = SlackIdentity(directory, lambda: spy, team_id=TEAM, bot_token="xoxb-1", http=down)
    assert await identity.resolve(TEAM, "UANN") is None
    assert spy.ttls and spy.ttls[-1] <= 300


def test_an_empty_signing_secret_in_the_environment_does_not_enable_slack():
    """BUG-FD-5: `HIVE_SLACK_SIGNING_SECRET=` (empty) enabled Slack with no secret at all, and
    every request then died with a KeyError (500) instead of the routes staying off."""
    settings = Settings(
        slack_signing_secret=SecretStr(""), slack_team_id=TEAM, slack_users={"U1": "a@acme.com"}
    )
    slack = env_defaults(settings)["slack"]
    assert slack.enabled is False and slack.secrets == {}


ODD_SHAPES = [None, "x", 5, [], [1], True]


@pytest.mark.parametrize("odd", ODD_SHAPES)
async def test_activities_with_fields_of_the_wrong_type_are_answered_not_crashed(
    stack, tmp_path, odd
):
    """BUG-FD-6: `value`, `from`, `conversation`, `channelData`, message payloads and parameters
    were assumed to be objects; a string, number or list raised AttributeError (HTTP 500)."""
    from hive_mcp.teams.handlers import TeamsBot

    directory = UserDirectory(AtomicJsonFile(tmp_path / "u.json", default=[]))
    directory.upsert(ANN)
    config = IntegrationConfig("teams", True, "tenant-1", "app", secrets={"app_password": "x"})

    class Outbox:
        async def reply(self, *args, **kwargs):
            return None

        async def member_email(self, *args, **kwargs):
            return None

    bot = TeamsBot(config, Outbox(), directory, lambda: stack, lambda: stack.backend.kv)  # type: ignore[arg-type]
    base = {
        "type": "invoke", "name": "composeExtension/query", "serviceUrl": "https://smba.trafficmanager.net/x/",
        "from": {"id": "29:a", "aadObjectId": "aad-ann"}, "conversation": {"id": "19:c"},
        "channelData": {"tenant": {"id": "tenant-1"}}, "value": {"commandId": "searchHive"},
    }  # fmt: skip
    for key in ("from", "conversation", "channelData", "value"):
        await bot.invoke({**base, key: odd})
        await bot.handle({**base, "type": "message", "text": "hi", key: odd})
    for name in (
        "composeExtension/query",
        "composeExtension/fetchTask",
        "composeExtension/submitAction",
    ):
        for inner in ("messagePayload", "data", "parameters"):
            value = {"commandId": "saveMessage", inner: odd}
            await bot.invoke({**base, "name": name, "value": value})
        payload = {"commandId": "saveMessage", "messagePayload": {"body": odd, "from": odd}}
        await bot.invoke({**base, "name": name, "value": payload})
        await bot.invoke({**base, "name": name, "value": {"parameters": [odd]}})


def test_real_teams_conversation_ids_can_be_mapped_to_a_project():
    """BUG-FD-7: a Teams channel id (`19:...@thread.tacv2`) was refused as a mapping key because
    '@' was not allowed, so no real Teams channel could be mapped to a project."""
    cid = "19:2da4c29f6d7041eca70b638b43d45437@thread.tacv2"
    IntegrationConfig("teams", channel_projects={cid: "p1"}).validate()
    with pytest.raises(Exception, match="bad channel"):
        IntegrationConfig("teams", channel_projects={"19:a b": "p1"}).validate()


async def test_a_channel_thread_reply_uses_the_channels_mapping(stack, tmp_path):
    """BUG-FD-8: in a channel Teams puts `;messageid=<post>` after the channel id, so the mapping
    was missed (the project's notes were not offered, saves went private) for every thread reply."""
    from hive_mcp.domain.scope import Scope, ScopeLevel
    from hive_mcp.services.models import RememberRequest
    from hive_mcp.teams.extension import TeamsExtension
    from hive_mcp.teams.handlers import TeamsBot

    directory = UserDirectory(AtomicJsonFile(tmp_path / "u.json", default=[]))
    directory.upsert(ANN)
    cid = "19:abc@thread.tacv2"
    config = IntegrationConfig(
        "teams", True, "tenant-1", "app", secrets={"app_password": "x"},
        channel_projects={cid: "p1"},
    )  # fmt: skip
    bot = TeamsBot(config, None, directory, lambda: stack, lambda: stack.backend.kv)  # type: ignore[arg-type]
    await stack.knowledge.remember(
        ANN.principal(),
        RememberRequest(
            title="Deploy p1", body="deploy steps", scope=Scope(ScopeLevel.PROJECT, "p1")
        ),
    )
    who = ANN.principal()
    assert bot._project(who, f"{cid};messageid=1700000000000") == "p1"
    ext = TeamsExtension(bot, "tenant-1", {cid: "p1"})
    activity = {"conversation": {"id": f"{cid};messageid=1700000000000"}}
    assert ext._shared_project(who, activity) == "p1"


async def test_an_unchanged_setting_does_not_rebuild_the_bot_every_refresh():
    """BUG-FD-9: the routes compared settings by identity, and the store re-reads them every five
    seconds, so a new bot was built each time: a new HTTP client, and for Teams the Bot Framework
    signing keys and the bot's access token were fetched again."""
    from hive_mcp.teams.routes import TeamsRoutes

    built = []

    def make(config):
        built.append(config)
        return object(), object()

    def fresh():
        return IntegrationConfig("teams", True, "t", "app", secrets={"app_password": "x"})

    async def get_config():
        return fresh()  # an equal copy each call, as the store returns after its cache expires

    class Req:
        headers = {}  # noqa: RUF012

        async def body(self):
            return b"x" * (256 * 1024 + 1)  # refused early: only the construction is of interest

    routes = TeamsRoutes(get_config, make, lambda: None)  # type: ignore[arg-type]
    await routes.messages(Req())  # type: ignore[arg-type]
    await routes.messages(Req())  # type: ignore[arg-type]
    assert len(built) == 1
