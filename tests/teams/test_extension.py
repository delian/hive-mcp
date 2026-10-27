"""The Teams message extension: search from the compose box, save a message, the app package."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.integrations.config import IntegrationConfig
from hive_mcp.security.directory import DirectoryUser, UserDirectory
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.services.models import RememberRequest
from hive_mcp.teams.extension import plain
from hive_mcp.teams.handlers import TeamsBot
from hive_mcp.teams.manifest import manifest, package
from hive_mcp.teams.routes import TeamsRoutes
from hive_mcp.teams.verify import BotFrameworkVerifier
from tests.teams.test_teams import APP, SERVICE, TENANT, Outbox, bot_token, keys_client

ANN = DirectoryUser("ann@acme.com", "org1", Role.MEMBER, projects=frozenset({"p1"}))


def invoke(  # noqa: PLR0913
    name, value, *, tenant=TENANT, aad="aad-ann", uid="29:ann", cid="19:c1"
):
    return {
        "type": "invoke", "name": name, "id": "i1", "serviceUrl": SERVICE, "value": value,
        "from": {"id": uid, "aadObjectId": aad},
        "conversation": {"id": cid, "conversationType": "channel"},
        "channelData": {"tenant": {"id": tenant}},
    }  # fmt: skip


def query(text, **kw):
    params = [{"name": "query", "value": text}]
    return invoke("composeExtension/query", {"commandId": "searchHive", "parameters": params}, **kw)


def save(payload, **kw):
    value = {"commandId": "saveMessage", "commandContext": "message", "messagePayload": payload}
    return invoke("composeExtension/fetchTask", value, **kw)


@pytest.fixture
def world(stack, tmp_path):
    directory = UserDirectory(AtomicJsonFile(tmp_path / "u.json", default=[]))
    directory.upsert(ANN)

    def make(**cfg) -> TeamsBot:
        config = IntegrationConfig("teams", True, TENANT, APP, secrets={"app_password": "x"}, **cfg)
        return TeamsBot(config, Outbox(), directory, lambda: stack, lambda: stack.backend.kv)  # type: ignore[arg-type]

    return make, stack


ADMIN = Principal("admin@acme.com", "org1", Role.ADMIN)


async def remember(stack, title, scope=None, body="deploy steps for the whole team"):
    who = ADMIN if scope and scope.level is ScopeLevel.ORG else ANN.principal()
    return await stack.knowledge.remember(who, RememberRequest(title=title, body=body, scope=scope))


async def test_search_offers_only_what_may_be_said_in_front_of_everyone(world):
    make, stack = world
    org = Scope(ScopeLevel.ORG, "org1")
    await remember(stack, "Deploy private")
    await remember(stack, "Deploy project p1", Scope(ScopeLevel.PROJECT, "p1"))
    await remember(stack, "Deploy for everyone", org)
    unmapped = (await make().invoke(query("deploy steps")))["composeExtension"]
    titles = [a["content"]["title"] for a in unmapped["attachments"]]
    assert titles == ["Deploy for everyone"]  # org-wide only: the conversation is not mapped
    mapped = await make(channel_projects={"19:c1": "p1"}).invoke(query("deploy steps"))
    titles = [a["content"]["title"] for a in mapped["composeExtension"]["attachments"]]
    assert "Deploy project p1" in titles and "Deploy for everyone" in titles
    assert "Deploy private" not in titles
    assert titles != [a["content"]["title"] for a in unmapped["attachments"]]


async def test_search_cards_are_hero_cards_with_previews_and_escaped_text(world):
    make, stack = world
    org = Scope(ScopeLevel.ORG, "org1")
    await remember(stack, "Links", org, body="see [click](http://evil.example) and <b>bold</b> _x_")
    response = (await make().invoke(query("see click bold")))["composeExtension"]
    assert response["type"] == "result" and response["attachmentLayout"] == "list"
    attachment = response["attachments"][0]
    assert attachment["contentType"] == "application/vnd.microsoft.card.hero"
    assert attachment["preview"]["content"] == attachment["content"]
    text = attachment["content"]["text"]
    assert "\\[click\\]\\(http" in text and "\\<b\\>" in text and "\\_x\\_" in text


async def test_search_answers_empty_queries_misses_strangers_and_other_tenants(world):
    make, _ = world
    bot = make()
    assert "Type what" in (await bot.invoke(query("   ")))["composeExtension"]["text"]
    initial = invoke(
        "composeExtension/query",
        {"commandId": "searchHive", "parameters": [{"name": "initialRun", "value": "true"}]},
    )
    assert "Type what" in (await bot.invoke(initial))["composeExtension"]["text"]
    assert (
        "Nothing shared" in (await bot.invoke(query("quantum pelican")))["composeExtension"]["text"]
    )
    stranger = await bot.invoke(query("x y", aad="aad-x", uid="29:nobody"))
    assert "could not match" in stranger["composeExtension"]["text"]
    foreign = await bot.invoke(query("deploy", tenant="other-tenant"))
    assert "not available" in foreign["composeExtension"]["text"]


async def test_saving_a_message_makes_a_private_note_once(world):
    make, stack = world
    payload = {
        "id": "1699", "subject": "Rollback tip",
        "body": {"content": "<p>Always roll back with <b>make rollback</b></p>", "contentType": "html"},
        "from": {"user": {"displayName": "Bob"}},
        "linkToMessage": "https://teams.microsoft.com/l/message/19:c1/1699",
    }  # fmt: skip
    first = await make().invoke(save(payload))
    assert first == {"task": {"type": "message", "value": "Saved as a private note: Rollback tip"}}
    again = await make().invoke(save(payload))
    assert again["task"]["value"].startswith("Already saved")
    found = await stack.knowledge.get(ANN.principal(), (await _only_note(stack)))
    assert "make rollback" in found.item.body and "<b>" not in found.item.body
    assert "from Bob in Teams" in found.item.body
    assert found.item.source == payload["linkToMessage"] and "source/teams" in found.item.tags
    assert found.item.scope.level is ScopeLevel.PRIVATE  # nobody else can read it


async def _only_note(stack):
    from hive_mcp.services.models import ConsultRequest

    res = await stack.consult.consult(ANN.principal(), ConsultRequest("rollback", limit=5))
    return res.primary[0].item_id


async def test_empty_messages_other_commands_and_other_invokes(world):
    make, _ = world
    bot = make()
    empty = await bot.invoke(save({"body": {"content": "<p> </p>"}}))
    assert empty["task"]["value"] == "That message has no text to save."
    other = invoke("composeExtension/fetchTask", {"commandId": "mystery"})
    assert (await bot.invoke(other))["task"]["value"] == "Unknown command."
    assert await bot.invoke(invoke("composeExtension/selectItem", {})) == {}
    assert await bot.invoke(invoke("task/submit", {})) == {}
    foreign = await bot.invoke(save({"body": {"content": "x"}}, tenant="other"))
    assert "not available" in foreign["task"]["value"]


def test_plain_escapes_markdown_and_collapses_whitespace():
    assert plain("a *b*\n\n[c](d)") == "a \\*b\\* \\[c\\]\\(d\\)"


# ---------------------------------------------------------------- the route
class InvokeBot:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def invoke(self, activity):
        self.calls.append(activity)
        return {"composeExtension": {"type": "message", "text": "ok"}}

    async def handle(self, activity) -> None:
        raise AssertionError("an invoke is not a message")


def test_the_route_returns_the_invoke_body_verifies_and_does_not_dedupe(stack):
    bot = InvokeBot()
    config = IntegrationConfig("teams", True, TENANT, APP, secrets={"app_password": "x"})
    verifier = BotFrameworkVerifier(APP, http=keys_client())

    async def get_config():
        return config

    client = TestClient(
        Starlette(
            routes=TeamsRoutes(
                get_config, lambda c: (verifier, bot), lambda: stack.backend.kv
            ).routes()
        )
    )
    headers = {"Authorization": f"Bearer {bot_token()}"}
    activity = query("deploy")
    for _ in range(2):  # Teams retries a slow invoke: asked again, answered again
        reply = client.post("/teams/messages", content=json.dumps(activity), headers=headers)
        assert reply.status_code == 200 and reply.json()["composeExtension"]["text"] == "ok"
    assert len(bot.calls) == 2
    bad = client.post(
        "/teams/messages", content=json.dumps(activity), headers={"Authorization": "Bearer x"}
    )
    assert bad.status_code == 401 and len(bot.calls) == 2


# ---------------------------------------------------------------- the package
def test_the_manifest_declares_the_bot_search_and_save_commands():
    m = manifest(APP, "https://hive.example.com:8443/")
    assert m["id"] == APP and m["validDomains"] == ["hive.example.com:8443"]
    assert m["bots"][0]["botId"] == APP and "groupChat" in m["bots"][0]["scopes"]
    commands = {c["id"]: c for c in m["composeExtensions"][0]["commands"]}
    assert commands["searchHive"]["type"] == "query" and commands["searchHive"]["parameters"]
    assert commands["saveMessage"]["type"] == "action"
    assert (
        commands["saveMessage"]["context"] == ["message"] and commands["saveMessage"]["fetchTask"]
    )
    assert m["icons"] == {"color": "color.png", "outline": "outline.png"}


def test_the_manifest_needs_https_and_an_app_id():
    for url in ("http://hive.example.com", "hive.example.com", ""):
        with pytest.raises(Exception, match="https"):
            manifest(APP, url)
    with pytest.raises(Exception, match="application"):
        manifest(" ", "https://hive.example.com")


def test_the_package_is_a_zip_with_the_manifest_and_two_valid_icons():
    archive = zipfile.ZipFile(
        io.BytesIO(package(APP, "https://hive.example.com", name="Acme Hive"))
    )
    assert sorted(archive.namelist()) == ["color.png", "manifest.json", "outline.png"]
    assert json.loads(archive.read("manifest.json"))["name"]["short"] == "Acme Hive"
    for name, size in (("color.png", 192), ("outline.png", 32)):
        data = archive.read(name)
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert int.from_bytes(data[16:20], "big") == size == int.from_bytes(data[20:24], "big")


def test_the_cli_writes_the_package_and_refuses_a_plain_http_address(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from hive_mcp.cli import cli

    out = tmp_path / "t.zip"
    monkeypatch.setenv("HIVE_PUBLIC_URL", "https://hive.example.com")
    ok = CliRunner().invoke(cli, ["teams-package", "--app-id", APP, "--out", str(out)])
    assert ok.exit_code == 0 and zipfile.is_zipfile(out)
    monkeypatch.setenv("HIVE_PUBLIC_URL", "http://localhost:8080")
    bad = CliRunner().invoke(cli, ["teams-package", "--app-id", APP, "--out", str(tmp_path / "x")])
    assert bad.exit_code == 1 and "https" in bad.output
