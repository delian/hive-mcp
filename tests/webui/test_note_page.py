"""The read-only note page that chat-app links open, and the sign-in that sends you back to it."""

from __future__ import annotations

import re

import httpx
import pytest

from tests.conformance.factory import ALICE, BOB
from tests.integration.conftest import Live
from tests.webui.test_admin_ui import browser, sign_in


@pytest.fixture
def ui(tmp_path):
    srv = Live(tmp_path / "data", admin_ui=True)
    srv.start()
    yield srv
    srv.stop()


async def make_note(
    live: Live, who, title="Deploy checklist", body="run <b>make deploy</b> & check"
):
    async with live.client(live.token_for(who)) as client:
        made = await client.call_tool("kb_remember", {"title": title, "body": body})
        return made.structured_content["id"]


async def test_a_signed_in_person_reads_their_own_note_escaped(ui: Live) -> None:
    note_id = await make_note(ui, ALICE)
    page = sign_in(ui, ALICE).get(f"/admin/n/{note_id}")
    assert page.status_code == 200 and "Deploy checklist" in page.text
    assert (
        "run &lt;b&gt;make deploy&lt;/b&gt; &amp; check" in page.text
        and "<b>make deploy" not in page.text
    )
    assert "your private notes" in page.text and "Revision" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in page.headers["content-security-policy"]


async def test_someone_who_may_not_read_it_gets_the_same_answer_as_for_a_missing_note(
    ui: Live,
) -> None:
    note_id = await make_note(ui, ALICE)
    bob = sign_in(ui, BOB)
    hidden = bob.get(f"/admin/n/{note_id}")
    missing = bob.get("/admin/n/" + "0" * 32)
    assert hidden.status_code == missing.status_code == 404
    assert hidden.text == missing.text and "Deploy checklist" not in hidden.text
    assert "does not exist, or you may not read it" in hidden.text


async def test_a_visitor_signs_in_and_lands_on_the_note(ui: Live) -> None:
    note_id = await make_note(ui, ALICE)
    client = browser(ui)
    first = client.get(f"/admin/n/{note_id}")
    assert first.status_code == 303 and first.headers["location"] == "/admin/login"
    cookie = first.headers["set-cookie"].lower()
    assert "hive_admin_next" in cookie and "httponly" in cookie and "path=/admin" in cookie
    token = ui.token_for(ALICE)
    done = client.post("/admin/login", data={"token": token})
    assert done.status_code == 303 and done.headers["location"] == f"/admin/n/{note_id}"
    assert "hive_admin_next" in done.headers.get_list("set-cookie")[0].lower() or any(
        "hive_admin_next" in c.lower() and "max-age=0" in c.lower()
        for c in done.headers.get_list("set-cookie")
    )  # the memory of where they were going is cleared
    assert client.get(f"/admin/n/{note_id}").status_code == 200
    again = browser(ui)
    again.post("/admin/login", data={"token": token})  # a plain sign-in goes home
    assert again.cookies.get("hive_admin_next") is None


@pytest.mark.parametrize(
    "path", ["/admin/n/short", "/admin/n/" + "g" * 32, "/admin/n/" + "A" * 32, "/admin/n/../login"]
)
def test_only_note_ids_are_remembered_for_after_sign_in(ui: Live, path: str) -> None:
    client = browser(ui)
    reply = client.get(path)
    assert "hive_admin_next" not in reply.headers.get("set-cookie", "")


def test_a_forged_return_cookie_cannot_redirect_anywhere_else(ui: Live) -> None:
    for evil in ("https://evil.example/", "//evil.example", "/admin/users", "/admin/n/zz"):
        client = httpx.Client(base_url=ui.url, headers={"Origin": ui.url})
        client.cookies.set("hive_admin_next", evil, path="/admin")
        done = client.post("/admin/login", data={"token": ui.token_for(ALICE)})
        assert done.headers["location"] == "/admin", evil


def test_the_note_page_needs_the_admin_ui_to_be_switched_on(live: Live) -> None:
    assert re.fullmatch(r"[0-9a-f]{32}", "0" * 32)
    assert httpx.get(f"{live.url}/admin/n/{'0' * 32}").status_code == 404
