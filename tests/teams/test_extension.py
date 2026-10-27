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

ANN = DirectoryUser("ann@acme.com", "org1", Role.MEMBER, projects=frozenset({"p1", "p2"}))
BOB = DirectoryUser("bob@acme.com", "org1", Role.MEMBER)  # in no project


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
    directory.upsert(BOB)

    def make(note_base=None, **cfg) -> TeamsBot:
        config = IntegrationConfig("teams", True, TENANT, APP, secrets={"app_password": "x"}, **cfg)
        return TeamsBot(
            config,
            Outbox(),
            directory,
            lambda: stack,
            lambda: stack.backend.kv,
            note_base=note_base,
        )  # type: ignore[arg-type]

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


PAYLOAD = {
    "id": "1699", "subject": "Rollback tip",
    "body": {"content": "<p>Always roll back with <b>make rollback</b></p>", "contentType": "html"},
    "from": {"user": {"displayName": "Bob"}},
    "linkToMessage": "https://teams.microsoft.com/l/message/19:c1/1699",
}  # fmt: skip


async def test_someone_in_no_project_saves_in_one_click_as_a_private_note_once(world):
    make, stack = world
    bob = {"aad": "aad-bob", "uid": "29:bob"}
    first = await make().invoke(save(PAYLOAD, **bob))
    assert first == {"task": {"type": "message", "value": "Saved as a private note: Rollback tip"}}
    again = await make().invoke(save(PAYLOAD, **bob))
    assert again["task"]["value"].startswith("Already saved")
    bob_principal = BOB.principal()
    found = await stack.knowledge.get(bob_principal, await _only_note(stack, bob_principal))
    assert "make rollback" in found.item.body and "<b>" not in found.item.body
    assert "from Bob in Teams" in found.item.body
    assert found.item.source == PAYLOAD["linkToMessage"] and "source/teams" in found.item.tags
    assert found.item.scope.level is ScopeLevel.PRIVATE  # nobody else can read it


def dialog_token(reply) -> str:
    card = reply["task"]["value"]["card"]["content"]
    return card["actions"][0]["data"]["token"]


async def test_someone_in_projects_gets_a_dialog_and_chooses_where_the_note_goes(world):
    make, stack = world
    bot = make()
    opened = await bot.invoke(save(PAYLOAD))
    value = opened["task"]["value"]
    assert opened["task"]["type"] == "continue" and value["title"] == "Save to Hive"
    card = value["card"]["content"]
    choices = card["body"][1]["choices"]
    assert [c["value"] for c in choices] == ["private", "p1", "p2"]
    assert card["body"][1]["value"] == "private" and card["actions"][0]["type"] == "Action.Submit"
    assert await _count_notes(stack) == 0  # nothing is saved until they press Save
    token = dialog_token(opened)

    def submit(project, token=token, **kw):
        data = {"token": token, "project": project}
        return invoke(
            "composeExtension/submitAction", {"commandId": "saveMessage", "data": data}, **kw
        )

    assert len(token) == 22  # 16 random bytes
    saved = await bot.invoke(submit("p2"))
    assert saved == {"task": {"type": "message", "value": "Saved in project p2: Rollback tip"}}
    note = await stack.knowledge.get(ANN.principal(), await _only_note(stack))
    assert note.item.scope == Scope(ScopeLevel.PROJECT, "p2") and note.item.title == "Rollback tip"
    assert "from Bob in Teams" in note.item.body and "make rollback" in note.item.body
    assert note.item.source == PAYLOAD["linkToMessage"]  # what the dialog held came through
    again = await bot.invoke(submit("p2"))  # Teams retries a slow submit: the same note
    assert again["task"]["value"].startswith("Already saved in project p2")
    other = await bot.invoke(submit("p1"))  # the same message kept in another project: another note
    assert other["task"]["value"] == "Saved in project p1: Rollback tip"
    private = await bot.invoke(submit("private"))
    assert private["task"]["value"] == "Saved as a private note: Rollback tip"
    assert await _count_notes(stack) == 3  # one per place


async def test_the_dialog_refuses_other_people_expired_tokens_and_foreign_projects(world):
    make, stack = world
    bot = make()
    token = dialog_token(await bot.invoke(save(PAYLOAD)))
    expired = "This dialog expired. Open Save to Hive on the message again."

    def submit(project, token=token, **kw):
        data = {"token": token, "project": project}
        return invoke(
            "composeExtension/submitAction", {"commandId": "saveMessage", "data": data}, **kw
        )

    assert (await bot.invoke(submit("p1", token="nope")))["task"]["value"] == expired
    assert (await bot.invoke(submit("p1", aad="aad-bob", uid="29:bob")))["task"]["value"] == expired
    assert (await bot.invoke(submit("p9")))["task"]["value"] == "You are not in project p9."
    assert (await bot.invoke(submit("")))["task"]["value"].startswith("Saved as a private note")
    assert await _count_notes(stack) == 1
    stack.backend.clock.advance(seconds=601)  # ten minutes: the held message is gone
    assert (await bot.invoke(submit("p1")))["task"]["value"] == expired


async def test_an_empty_message_gets_no_dialog(world):
    make, _ = world
    reply = await make().invoke(save({"body": {"content": "<p> </p>"}}))
    assert reply == {"task": {"type": "message", "value": "That message has no text to save."}}


async def _count_notes(stack) -> int:
    from hive_mcp.domain.item import ItemQuery

    page = await stack.knowledge.browse(
        ANN.principal(), ItemQuery(include_referenced=False, limit=50)
    )
    return len(page.items)


async def _only_note(stack, principal=None):
    from hive_mcp.services.models import ConsultRequest

    res = await stack.consult.consult(
        principal or ANN.principal(), ConsultRequest("rollback", limit=5)
    )
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
    developer = m["developer"]
    assert developer["privacyUrl"] == "https://hive.example.com:8443/legal/privacy"
    assert developer["termsOfUseUrl"] == "https://hive.example.com:8443/legal/terms"
    assert "messageHandlers" not in m["composeExtensions"][0]  # off unless note pages exist


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


def test_your_own_legal_urls_replace_ours_and_link_unfurling_names_only_the_host():
    m = manifest(
        APP, "https://hive.example.com:8443/", privacy_url="https://acme.com/p",
        terms_url="https://acme.com/t", link_unfurling=True,
    )  # fmt: skip
    assert m["developer"]["privacyUrl"] == "https://acme.com/p"
    assert m["developer"]["termsOfUseUrl"] == "https://acme.com/t"
    assert m["composeExtensions"][0]["messageHandlers"] == [
        {"type": "link", "value": {"domains": ["hive.example.com"]}}
    ]
    assert m["validDomains"] == ["hive.example.com:8443"]


def test_the_cli_turns_unfurling_on_with_the_admin_ui_and_passes_the_legal_urls(
    tmp_path, monkeypatch
):
    from typer.testing import CliRunner

    from hive_mcp.cli import cli

    monkeypatch.setenv("HIVE_PUBLIC_URL", "https://hive.example.com")
    monkeypatch.setenv("HIVE_PRIVACY_URL", "https://acme.com/privacy")

    def built(*extra, admin_ui=None):
        if admin_ui is not None:
            monkeypatch.setenv("HIVE_ADMIN_UI", str(admin_ui).lower())
        out = tmp_path / "t.zip"
        run = CliRunner().invoke(cli, ["teams-package", "--app-id", APP, "--out", str(out), *extra])
        assert run.exit_code == 0, run.output
        return json.loads(zipfile.ZipFile(out).read("manifest.json"))

    off = built(admin_ui=False)
    assert "messageHandlers" not in off["composeExtensions"][0]
    assert off["developer"]["privacyUrl"] == "https://acme.com/privacy"
    assert off["developer"]["termsOfUseUrl"] == "https://hive.example.com/legal/terms"
    on = built(admin_ui=True)
    assert on["composeExtensions"][0]["messageHandlers"][0]["value"]["domains"] == [
        "hive.example.com"
    ]
    assert "messageHandlers" not in built("--no-link-unfurling")["composeExtensions"][0]
    monkeypatch.setenv("HIVE_ADMIN_UI", "false")
    assert "messageHandlers" in built("--link-unfurling")["composeExtensions"][0]


async def test_a_submit_without_a_token_or_a_choice_is_handled_not_guessed(world):
    make, stack = world
    bot = make()
    token = dialog_token(await bot.invoke(save(PAYLOAD)))
    kv = stack.backend.kv
    for guess in ("None", "XXXX"):  # what a sloppy default would look up
        await kv.put(
            "teams:save", guess, {"user": "ann@acme.com", "payload": PAYLOAD}, ttl_seconds=60
        )
    no_token = invoke("composeExtension/submitAction", {"commandId": "saveMessage", "data": {}})
    expired = "This dialog expired. Open Save to Hive on the message again."
    assert (await bot.invoke(no_token))["task"]["value"] == expired
    no_choice = invoke(
        "composeExtension/submitAction", {"commandId": "saveMessage", "data": {"token": token}}
    )
    assert (await bot.invoke(no_choice))["task"]["value"] == "Saved as a private note: Rollback tip"
