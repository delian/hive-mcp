"""Pasted note links unfurl into cards, and search cards get an Open button."""

from __future__ import annotations

from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.services.models import RememberRequest
from tests.teams.test_extension import ANN, BOB, invoke, query, world  # noqa: F401

BASE = "https://hive.example.com/admin/n/"
ADMIN = Principal("admin@acme.com", "org1", Role.ADMIN)


def link(url, **kw):
    return invoke("composeExtension/queryLink", {"url": url}, **kw)


async def note(stack, title, who=None, scope=None, body="deploy steps for the whole team"):
    who = who or ANN.principal()
    made = await stack.knowledge.remember(who, RememberRequest(title=title, body=body, scope=scope))
    return made.item.id


def cards(reply):
    return reply["composeExtension"]["attachments"]


async def test_an_organisation_note_unfurls_with_its_text_and_an_open_button(world):
    make, stack = world
    org_id = await note(
        stack, "Deploy for everyone", ADMIN, Scope(ScopeLevel.ORG, "org1"), "*bold* steps"
    )
    reply = await make(note_base=BASE).invoke(link(BASE + org_id))
    [shown] = cards(reply)
    assert shown["contentType"] == "application/vnd.microsoft.card.hero"
    assert shown["content"] == {
        "title": "Deploy for everyone", "subtitle": "organisation\\-wide knowledge",
        "text": "\\*bold\\* steps",
        "buttons": [{"type": "openUrl", "title": "Open in Hive", "value": BASE + org_id}],
    }  # fmt: skip
    assert "buttons" not in shown["preview"]["content"]  # a preview may not carry buttons
    assert reply["composeExtension"]["type"] == "result"
    assert reply["composeExtension"]["attachmentLayout"] == "list"
    assert reply["composeExtension"]["suggestedActions"] == {
        "actions": [{"type": "setCachePolicy", "value": '{"type":"no-cache"}'}]
    }  # what it shows depends on who asks: never cached for others


async def test_private_and_unreadable_and_missing_notes_all_unfurl_to_the_same_bare_card(world):
    make, stack = world
    private = await note(stack, "Secret plan", body="do not tell anyone")
    other_project = await note(stack, "Project note", scope=Scope(ScopeLevel.PROJECT, "p1"))
    bot = make(note_base=BASE)
    generic = None
    for note_id, who in (
        (private, {}), (other_project, {}), ("0" * 32, {}), (private, {"aad": "aad-bob", "uid": "29:bob"}),
    ):  # fmt: skip
        reply = await bot.invoke(link(BASE + note_id, **who))
        assert (
            reply["composeExtension"]["suggestedActions"]["actions"][0]["type"] == "setCachePolicy"
        )
        [shown] = cards(reply)
        assert shown["content"]["title"] == "Hive note" and "Secret" not in str(shown)
        assert shown["content"]["text"] == "Open it in Hive to read it (sign-in required)."
        assert shown["content"]["subtitle"] == ""
        assert shown["content"]["buttons"][0]["value"] == BASE + note_id
        generic = generic or shown["content"]["text"]
    assert generic


async def test_a_project_note_unfurls_in_the_conversation_mapped_to_that_project(world):
    make, stack = world
    note_id = await note(stack, "Project note", scope=Scope(ScopeLevel.PROJECT, "p1"))
    mapped = make(note_base=BASE, channel_projects={"19:c1": "p1"})
    [shown] = cards(await mapped.invoke(link(BASE + note_id)))
    assert (
        shown["content"]["title"] == "Project note" and shown["content"]["subtitle"] == "project p1"
    )
    elsewhere = make(note_base=BASE, channel_projects={"19:c1": "p2"})  # another project's channel
    [bare] = cards(await elsewhere.invoke(link(BASE + note_id)))
    assert bare["content"]["title"] == "Hive note"
    gone = await note(stack, "Old note", scope=Scope(ScopeLevel.PROJECT, "p1"))
    from hive_mcp.domain.item import ItemPatch, Status

    await stack.knowledge.update(ANN.principal(), gone, ItemPatch(status=Status.ARCHIVED), 1)
    [archived] = cards(await mapped.invoke(link(BASE + gone)))
    assert archived["content"]["title"] == "Hive note"  # an archived note is not quoted


async def test_links_that_are_not_ours_get_an_empty_answer(world):
    make, stack = world
    real = await note(stack, "x", ADMIN, Scope(ScopeLevel.ORG, "org1"))
    bot = make(note_base=BASE)
    for url in (
        "https://evil.example/admin/n/" + real, BASE + "short", BASE + real + "x", BASE + real.upper(),
        BASE + real + "X", BASE[:-1] + real, "", "https://hive.example.com/admin/n/", BASE + real + "/extra",
    ):  # fmt: skip
        assert await bot.invoke(link(url)) == {}, url
    assert cards(await bot.invoke(link(BASE + real + "/")))  # one trailing slash is fine
    assert await make().invoke(link(BASE + real)) == {}  # no note pages on this server


async def test_link_queries_check_tenant_and_identity_like_everything_else(world):
    make, stack = world
    real = await note(stack, "x", ADMIN, Scope(ScopeLevel.ORG, "org1"))
    bot = make(note_base=BASE)
    foreign = await bot.invoke(link(BASE + real, tenant="other"))
    assert foreign == {
        "composeExtension": {
            "type": "message",
            "text": "This app is not available to your organisation.",
        }
    }
    stranger = await bot.invoke(link(BASE + real, aad="aad-x", uid="29:nobody"))
    assert stranger["composeExtension"]["text"].startswith("I could not match")


async def test_search_cards_get_an_open_button_only_when_notes_have_a_page(world):
    make, stack = world
    org_id = await note(stack, "Deploy for everyone", ADMIN, Scope(ScopeLevel.ORG, "org1"))
    with_pages = (await make(note_base=BASE).invoke(query("deploy steps")))["composeExtension"]
    assert with_pages["attachments"][0]["content"]["buttons"] == [
        {"type": "openUrl", "title": "Open in Hive", "value": BASE + org_id}
    ]
    assert "suggestedActions" not in with_pages
    without = (await make().invoke(query("deploy steps")))["composeExtension"]
    assert "buttons" not in without["attachments"][0]["content"]
    assert BOB.email == "bob@acme.com"
