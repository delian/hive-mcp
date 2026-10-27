"""The message extension's exact answers: shapes, limits, defaults and what each branch says."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.item import Kind, Status
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.services.models import Match
from hive_mcp.teams.extension import SAVE, TeamsExtension, card, message, plain, task_message
from tests.teams.test_extension import ANN, invoke, query, save, world  # noqa: F401

WHO = Principal("ann@acme.com", "org1", Role.MEMBER, projects=frozenset({"p1"}))
HERO = "application/vnd.microsoft.card.hero"


def match(title, scope, tier="primary", snippet="a snippet", provenance="your notes"):
    return Match(
        item_id=title, title=title, snippet=snippet, kind=Kind.NOTE, tags=frozenset(),
        status=Status.ACTIVE, score=1.0, tier=SimpleNamespace(value=tier), scope=scope,  # type: ignore[arg-type]
        author_id="x", updated_at=datetime.now(UTC), revision=1, provenance=provenance,
    )  # fmt: skip


class FakeBot:
    def __init__(self, who=WHO, matches=(), error: Exception | None = None) -> None:
        self.who, self.matches, self.error = who, list(matches), error
        self.requests: list = []
        self.saved: list = []
        bot = self

        class Consult:
            async def consult(self, principal, request):
                bot.requests.append((principal, request))
                if bot.error:
                    raise bot.error
                return SimpleNamespace(primary=bot.matches)

        class Knowledge:
            async def remember(self, principal, request):
                bot.saved.append((principal, request))
                if bot.error:
                    raise bot.error
                item = SimpleNamespace(title=request.title)
                return SimpleNamespace(created=True, item=item)

        self._services = SimpleNamespace(consult=Consult(), knowledge=Knowledge())

    async def resolve(self, _activity):
        return self.who

    def services(self):
        return self._services


def ext(bot, projects=None):
    return TeamsExtension(bot, "tenant-1", projects or {})  # type: ignore[arg-type]


def act(name, value=None, *, tenant="tenant-1", conversation="19:c1"):
    out = {"type": "invoke", "name": name, "value": value or {}}
    out["channelData"] = {"tenant": {"id": tenant}}
    if conversation is not None:
        out["conversation"] = {"id": conversation}
    return out


def test_the_small_builders_have_the_exact_shapes_teams_expects():
    assert message("hi") == {"composeExtension": {"type": "message", "text": "hi"}}
    assert task_message("hi") == {"task": {"type": "message", "value": "hi"}}
    built = card("T" * 300, "S" * 300, "body")
    assert built["contentType"] == HERO
    assert built["content"] == {"title": "T" * 200, "subtitle": "S" * 100, "text": "body"}
    assert built["preview"] == {"contentType": HERO, "content": built["content"]}
    assert plain("a\\b `c` #d |e| ~f~ g! h. i+ j- {k}") == (
        "a\\\\b \\`c\\` \\#d \\|e\\| \\~f\\~ g\\! h\\. i\\+ j\\- \\{k\\}"
    )


async def test_other_activity_names_get_an_empty_answer_and_a_missing_name_too():
    bot = ext(FakeBot())
    assert await bot.invoke({"type": "invoke"}) == {}
    assert await bot.invoke(act("composeExtension/submitAction")) == {}


async def test_the_tenant_and_the_user_checks_answer_in_each_commands_own_form():
    refused = "This app is not available to your organisation."
    assert await ext(FakeBot()).invoke(act("composeExtension/query", tenant="x")) == message(
        refused
    )
    assert await ext(FakeBot()).invoke(act("composeExtension/fetchTask", tenant="x")) == (
        task_message(refused)
    )
    nobody = FakeBot(who=None)
    reply = await ext(nobody).invoke(act("composeExtension/query"))
    assert reply["composeExtension"]["text"].startswith("I could not match your Teams account")
    assert (await ext(nobody).invoke(act("composeExtension/fetchTask")))["task"][
        "value"
    ].startswith("I could not match")
    assert await ext(FakeBot()).invoke({"type": "invoke", "name": "composeExtension/query"}) == (
        message(refused)
    )  # no channel data at all


async def test_domain_errors_become_a_sorry_in_the_right_form_and_other_errors_propagate():
    failing = FakeBot(error=ValidationError("boom"))
    query_value = {"parameters": [{"name": "query", "value": "deploy"}]}
    assert await ext(failing).invoke(act("composeExtension/query", query_value)) == (
        message("Sorry: boom")
    )
    value = {"commandId": SAVE, "messagePayload": {"body": {"content": "text"}}}
    assert await ext(failing).invoke(act("composeExtension/fetchTask", value)) == (
        task_message("Sorry: boom")
    )


async def test_search_reads_its_parameters_defensively_and_asks_for_twenty_results():
    bot = FakeBot()
    e = ext(bot)
    only_name = {"parameters": [{"name": "query"}, {"value": "no name"}]}  # no value / no name
    assert (
        "Type what"
        in (await e.invoke(act("composeExtension/query", only_name)))["composeExtension"]["text"]
    )
    assert (await e.invoke(act("composeExtension/query", {})))["composeExtension"]["text"] == (
        "Type what you are looking for in the team's shared knowledge."
    )
    initial = {
        "parameters": [{"name": "initialRun", "value": "true"}, {"name": "query", "value": "x"}]
    }
    assert (
        "Type what"
        in (await e.invoke(act("composeExtension/query", initial)))["composeExtension"]["text"]
    )
    assert bot.requests == []  # nothing searched so far
    not_initial = {
        "parameters": [{"name": "initialRun", "value": "TRUE"}, {"name": "query", "value": "x y"}]
    }
    await e.invoke(act("composeExtension/query", not_initial))
    principal, request = bot.requests[-1]
    assert principal == WHO and request.text if hasattr(request, "text") else True
    assert request.query == "x y" and request.limit == 20
    await e.invoke(
        act("composeExtension/query", {"parameters": [{"name": "query", "value": "q" * 500}]})
    )
    assert len(bot.requests[-1][1].query) == 200
    await e.invoke(
        act("composeExtension/query", {"parameters": [{"name": "query", "value": " <b>hi</b> "}]})
    )
    assert bot.requests[-1][1].query == "hi"


async def test_search_answers_with_at_most_ten_cards_of_shared_knowledge_only():
    org, p1, p2 = (
        Scope(ScopeLevel.ORG, "org1"), Scope(ScopeLevel.PROJECT, "p1"), Scope(ScopeLevel.PROJECT, "p2"),
    )  # fmt: skip
    many = [match(f"org {i}", org, snippet="*bold*", provenance="org-wide") for i in range(14)]
    bot = FakeBot(
        matches=[*many, match("proj", p1), match("other proj", p2), match("ref", org, "referenced")]
    )
    value = {"parameters": [{"name": "query", "value": "deploy"}]}
    reply = (await ext(bot).invoke(act("composeExtension/query", value)))["composeExtension"]
    assert reply["type"] == "result" and reply["attachmentLayout"] == "list"
    assert len(reply["attachments"]) == 10
    first = reply["attachments"][0]["content"]
    assert first == {"title": "org 0", "subtitle": "org\\-wide", "text": "\\*bold\\*"}
    few = FakeBot(
        matches=[
            match("org a", org),
            match("proj", p1),
            match("other proj", p2),
            match("ref", org, "referenced"),
        ]
    )
    mapped = ext(few, {"19:c1": "p1"})
    reply = await mapped.invoke(act("composeExtension/query", value))
    titles = [a["content"]["title"] for a in reply["composeExtension"]["attachments"]]
    assert titles == ["org a", "proj"]
    # a conversation mapped to a project the person is not in does not widen what is offered
    foreign = ext(FakeBot(matches=[match("other proj", p2)]), {"19:c1": "p2"})
    assert (await foreign.invoke(act("composeExtension/query", value)))["composeExtension"][
        "text"
    ] == ("Nothing shared with the whole team found for: deploy")
    nothing = ext(FakeBot(matches=[match("proj", p1)]), {"19:c1": "p1"})
    no_conversation = act("composeExtension/query", value, conversation=None)
    assert "Nothing shared" in (await nothing.invoke(no_conversation))["composeExtension"]["text"]


async def test_snippets_are_cut_at_three_hundred_characters():
    org = Scope(ScopeLevel.ORG, "org1")
    bot = FakeBot(matches=[match("long", org, snippet="word " * 200)])
    value = {"parameters": [{"name": "query", "value": "w"}]}
    attachments = (await ext(bot).invoke(act("composeExtension/query", value)))["composeExtension"][
        "attachments"
    ]
    assert len(attachments[0]["content"]["text"]) == 300


def saved(bot):
    return bot.saved[-1][1]


async def test_saving_defaults_every_missing_part_and_keys_the_note_to_the_message():
    bot = FakeBot()
    e = ext(bot)

    def fetch(payload):
        return act("composeExtension/fetchTask", {"commandId": SAVE, "messagePayload": payload})

    assert (await e.invoke(fetch({})))["task"]["value"] == "That message has no text to save."
    assert (await e.invoke(fetch({"body": {}})))["task"][
        "value"
    ] == "That message has no text to save."
    assert (await e.invoke(fetch({"body": {"content": "<br>"}})))["task"]["value"] == (
        "That message has no text to save."
    )
    assert bot.saved == []
    ok = await e.invoke(fetch({"body": {"content": "First line\nsecond"}, "id": "m7"}))
    assert ok == task_message("Saved as a private note: First line")
    request = saved(bot)
    assert request.title == "First line" and request.body == "First line\nsecond"
    assert request.source == "teams:message:m7" and request.tags == ("source/teams",)
    assert request.scope is None  # private to the sender
    key_a = request.idempotency_key
    assert key_a.startswith("teams-msg:") and key_a.endswith(f":{WHO.id}")
    await e.invoke(fetch({"body": {"content": "First line\nsecond"}, "id": "m8"}))
    assert saved(bot).idempotency_key == key_a  # no link: the text decides
    await e.invoke(fetch({"body": {"content": "other"}, "linkToMessage": "https://t/1"}))
    key_b = saved(bot).idempotency_key
    await e.invoke(fetch({"body": {"content": "changed text"}, "linkToMessage": "https://t/1"}))
    assert saved(bot).idempotency_key == key_b  # a link decides before the text
    assert saved(bot).source == "https://t/1"


async def test_the_title_the_sender_and_the_limits():
    bot = FakeBot()
    e = ext(bot)

    def fetch(payload):
        return act("composeExtension/fetchTask", {"commandId": SAVE, "messagePayload": payload})

    await e.invoke(fetch({
        "subject": "<b>Subject line</b>", "body": {"content": "body text"},
        "from": {"user": {"displayName": "Bob"}},
    }))  # fmt: skip
    request = saved(bot)
    assert request.title == "Subject line" and request.body == "body text\n\n(from Bob in Teams)"
    await e.invoke(fetch({"subject": "", "body": {"content": "x" * 200}, "from": {"user": {}}}))
    assert len(saved(bot).title) == 80 and "(from" not in saved(bot).body
    await e.invoke(fetch({"subject": None, "body": {"content": "y" * 30000}, "from": None}))
    assert len(saved(bot).body) == 20000
    await e.invoke(fetch({"body": {"content": "z"}, "linkToMessage": "L" * 500}))
    assert len(saved(bot).source) == 400
    unknown = await e.invoke(act("composeExtension/fetchTask", {"commandId": "other"}))
    assert unknown == task_message("Unknown command.")


async def test_a_missing_conversation_id_is_not_looked_up_as_text_and_ids_default_to_empty():
    org = Scope(ScopeLevel.ORG, "org1")
    p1 = Scope(ScopeLevel.PROJECT, "p1")
    bot = FakeBot(matches=[match("proj", p1), match("org a", org)])
    value = {"parameters": [{"name": "query", "value": "x"}]}
    tricky = ext(bot, {"None": "p1", "XXXX": "p1"})
    reply = await tricky.invoke(act("composeExtension/query", value, conversation=None))
    assert [a["content"]["title"] for a in reply["composeExtension"]["attachments"]] == ["org a"]
    no_id = {**act("composeExtension/query", value), "conversation": {}}
    reply = await tricky.invoke(no_id)
    assert [a["content"]["title"] for a in reply["composeExtension"]["attachments"]] == ["org a"]


async def test_saving_a_message_without_an_id_or_link_and_the_digest_shape():
    bot = FakeBot()
    e = ext(bot)
    fetch = act(
        "composeExtension/fetchTask",
        {"commandId": SAVE, "messagePayload": {"body": {"content": "hello"}}},
    )
    await e.invoke(fetch)
    request = bot.saved[-1][1]
    assert request.source == "teams:message:"
    import re

    assert re.fullmatch(r"teams-msg:[0-9a-f]{32}:ann@acme\.com", request.idempotency_key)
