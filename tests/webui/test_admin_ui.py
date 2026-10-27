"""The admin pages over real HTTP: sign-in, access by role, CSRF, approvals, audit and usage."""

from __future__ import annotations

import re
import time
from html import unescape
from typing import TYPE_CHECKING

import httpx
import pytest

from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.security.directory import DirectoryUser
from tests.conformance.factory import ALICE, BOB, ROOT
from tests.integration.conftest import Live, _oauth_server

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from tests.integration.fake_google import FakeGoogle

APPROVER = Principal(
    "tina",
    "org1",
    Role.MEMBER,
    teams=frozenset({"t1"}),
    approves=frozenset({Scope(ScopeLevel.TEAM, "t1")}),
)


@pytest.fixture
def ui(tmp_path: Path) -> Iterator[Live]:
    srv = Live(tmp_path / "data", admin_ui=True)
    srv.start()
    yield srv
    srv.stop()


def browser(live: Live) -> httpx.Client:
    return httpx.Client(base_url=live.url, headers={"Origin": live.url})


def sign_in(live: Live, who: Principal, **kw: object) -> httpx.Client:
    client = browser(live)
    token = live.token_for(who, **kw)
    response = client.post("/admin/login", data={"token": token})
    assert response.status_code == 303 and response.headers["location"] == "/admin"
    return client


def csrf(client: httpx.Client, path: str = "/admin/promotions") -> str:
    match = re.search(r"name=csrf value='([^']+)'", client.get(path).text)
    assert match
    return unescape(match.group(1))


async def make_request(live: Live, title: str = "Dwell time plan") -> str:
    async with live.client(live.token_for(ALICE)) as alice:
        item = (
            await alice.call_tool("kb_remember", {"title": title, "body": "Use 12 ms dwell."})
        ).structured_content
        req = (
            await alice.call_tool(
                "kb_request_promotion", {"id": item["id"], "to": "team", "owner": "t1"}
            )
        ).structured_content
    return req["id"]


# ---------------------------------------------------------------- off by default / sign-in
def test_the_admin_ui_is_off_unless_enabled(live: Live) -> None:
    assert httpx.get(f"{live.url}/admin").status_code == 404


def test_pages_need_a_session_and_headers_are_strict(ui: Live) -> None:
    r = browser(ui).get("/admin/promotions")
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    page = browser(ui).get("/admin/login")
    assert page.status_code == 200 and "script" not in page.text.lower()
    csp = page.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert page.headers["x-frame-options"] == "DENY" and page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "same-origin"  # keeps Origin on form posts


def test_bad_tokens_are_refused_and_cookie_is_hardened(ui: Live) -> None:
    client = browser(ui)
    for token in ("", "nope", "sk-" + "a" * 40, "hive_unknown"):
        assert client.post("/admin/login", data={"token": token}).status_code == 401
    token = ui.token_for(ADMIN := ROOT, label="root-tok")
    ok = client.post("/admin/login", data={"token": token})
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/admin" in cookie
    assert ADMIN.id in client.get("/admin").text


def test_login_from_another_origin_is_refused(ui: Live) -> None:
    token = ui.token_for(ROOT)
    for headers in ({"Origin": "https://evil.example"}, {}):
        client = httpx.Client(base_url=ui.url)
        response = client.post("/admin/login", data={"token": token}, headers=headers)
        assert response.status_code == 401 and not client.cookies


def test_an_expired_or_revoked_token_ends_the_session(ui: Live) -> None:
    client = sign_in(ui, ROOT, label="gone")
    assert client.get("/admin/usage").status_code == 200
    ui.tokens.revoke("gone")
    assert client.get("/admin/usage").headers["location"] == "/admin/login"
    short = browser(ui)
    token = ui.token_for(ROOT, label="short", expires_in_days=0.00001)
    time.sleep(1.2)
    assert short.post("/admin/login", data={"token": token}).status_code == 401


def test_a_tampered_cookie_is_ignored(ui: Live) -> None:
    client = sign_in(ui, ROOT)
    value = client.cookies["hive_admin"]
    payload, _, mac = value.partition(".")
    for forged in (f"{payload}.{'0' * len(mac)}", f"{payload}x.{mac}", payload, "junk"):
        fresh = browser(ui)
        fresh.cookies.set("hive_admin", forged, path="/admin")
        assert fresh.get("/admin").status_code == 303


def test_logout_needs_the_csrf_token_and_ends_the_session(ui: Live) -> None:
    client = sign_in(ui, ROOT)
    token = csrf(client, "/admin")
    assert client.post("/admin/logout", data={"csrf": "wrong"}).headers["location"] == "/admin"
    foreign = client.post(
        "/admin/logout", data={"csrf": token}, headers={"Origin": "https://evil.example"}
    )
    assert foreign.headers["location"] == "/admin"
    assert client.get("/admin/usage").status_code == 200  # still signed in
    assert client.post("/admin/logout", data={"csrf": token}).headers["location"] == "/admin/login"
    client.cookies.clear()
    assert client.get("/admin/usage").status_code == 303


def test_a_stolen_cookie_dies_with_the_sign_out(ui: Live) -> None:
    client = sign_in(ui, ROOT)
    stolen = httpx.Client(base_url=ui.url, headers={"Origin": ui.url})
    stolen.cookies.set("hive_admin", client.cookies["hive_admin"], path="/admin")
    assert stolen.get("/admin/usage").status_code == 200
    token = csrf(client, "/admin")
    assert client.post("/admin/logout", data={"csrf": token}).status_code == 303
    assert stolen.get("/admin/usage").status_code == 303  # the copy no longer works


# ---------------------------------------------------------------- roles
def test_pages_follow_the_role(ui: Live) -> None:
    member = sign_in(ui, BOB)
    home = member.get("/admin")
    assert home.status_code == 200 and "no admin pages" in home.text
    for path in ("/admin/promotions", "/admin/audit", "/admin/usage"):
        assert member.get(path).status_code == 403
    approver = sign_in(ui, APPROVER)
    assert approver.get("/admin/promotions").status_code == 200
    assert approver.get("/admin/audit").status_code == 403  # approvers are not admins
    assert approver.get("/admin/usage").status_code == 403
    admin = sign_in(ui, ROOT)
    assert all(admin.get(p).status_code == 200 for p in ("/admin/audit", "/admin/usage"))


# ---------------------------------------------------------------- approvals
async def test_an_approver_reviews_and_approves_a_request(ui: Live) -> None:
    promo = await make_request(ui)
    client = sign_in(ui, APPROVER)
    page = client.get("/admin/promotions").text
    assert "Dwell time plan" in page and "Use 12 ms dwell." in page and "team:t1" in page
    token = csrf(client)
    forged = client.post(
        "/admin/promotions/decide", data={"csrf": "x", "promotion": promo, "decision": "approve"}
    )
    assert forged.status_code == 403 and "Dwell time plan" in client.get("/admin/promotions").text
    cross = client.post(
        "/admin/promotions/decide",
        data={"csrf": token, "promotion": promo, "decision": "approve"},
        headers={"Origin": "https://evil.example"},
    )
    assert cross.status_code == 403
    done = client.post(
        "/admin/promotions/decide",
        data={"csrf": token, "promotion": promo, "decision": "approve", "note": "ok"},
    )
    assert done.status_code == 303 and done.headers["location"].endswith("done=approve")
    assert "approved" in client.get(done.headers["location"]).text
    assert "Dwell time plan" not in client.get("/admin/promotions").text
    again = client.post(
        "/admin/promotions/decide",
        data={"csrf": token, "promotion": promo, "decision": "approve"},
    )
    assert again.status_code == 400 and "already decided" in again.text


async def test_rejecting_and_unknown_decisions(ui: Live) -> None:
    promo = await make_request(ui)
    client = sign_in(ui, ROOT)
    token = csrf(client)
    for decision in ("withdraw", "<script>", ""):
        r = client.post(
            "/admin/promotions/decide",
            data={"csrf": token, "promotion": promo, "decision": decision},
        )
        assert r.status_code == 400
    assert (
        client.post(
            "/admin/promotions/decide",
            data={"csrf": token, "promotion": "nope", "decision": "reject"},
        ).status_code
        == 404
    )
    done = client.post(
        "/admin/promotions/decide", data={"csrf": token, "promotion": promo, "decision": "reject"}
    )
    assert done.status_code == 303
    async with ui.client(ui.token_for(ALICE)) as alice:
        rows = (await alice.call_tool("kb_list_promotions", {"status": "all"})).structured_content
    assert [r["status"] for r in rows["result"]] == ["rejected"]


async def test_an_approver_of_another_team_cannot_decide(ui: Live) -> None:
    promo = await make_request(ui)
    other = Principal(
        "olga", "org1", Role.MEMBER, approves=frozenset({Scope(ScopeLevel.TEAM, "t9")})
    )
    client = sign_in(ui, other)
    assert "Dwell time plan" not in client.get("/admin/promotions").text
    r = client.post(
        "/admin/promotions/decide",
        data={"csrf": csrf(client), "promotion": promo, "decision": "approve"},
    )
    assert r.status_code in (403, 404)


async def test_item_text_is_escaped(ui: Live) -> None:
    await make_request(ui, title="<script>alert(1)</script>")
    page = sign_in(ui, APPROVER).get("/admin/promotions").text
    assert "<script>alert" not in page and "&lt;script&gt;alert(1)" in page


# ---------------------------------------------------------------- audit and usage
async def test_audit_filters_and_pages(ui: Live) -> None:
    await make_request(ui)
    await make_request(ui, title="Second")
    admin = sign_in(ui, ROOT)
    page = admin.get("/admin/audit").text
    assert "promotion.request" in page and "admin.login" in page
    only = admin.get("/admin/audit", params={"action": "promotion.request"}).text
    assert only.count("promotion.request") >= 2 and "admin.login" not in only
    assert "alice" in admin.get("/admin/audit", params={"actor": "alice"}).text
    assert "promotion.request" not in admin.get("/admin/audit", params={"actor": "bob"}).text
    assert admin.get("/admin/audit", params={"before": "junk"}).status_code == 200
    assert "promotion.request" not in admin.get("/admin/audit", params={"before": "1"}).text
    assert "Newest first" in page


async def test_usage_lists_authors(ui: Live) -> None:
    await make_request(ui)
    page = sign_in(ui, ROOT).get("/admin/usage").text
    assert "alice" in page and "unlimited" in page


def test_quota_limits_are_shown(tmp_path: Path) -> None:
    srv = Live(tmp_path / "data", admin_ui=True, quota_items_per_user=7)
    srv.start()
    try:
        assert "7 items" in sign_in(srv, ROOT).get("/admin/usage").text
    finally:
        srv.stop()


# ---------------------------------------------------------------- Google
@pytest.fixture
def google_ui(tmp_path: Path, google: FakeGoogle) -> Iterator[Live]:
    srv = _oauth_server(tmp_path, google, admin_ui=True)
    srv.directory.upsert(DirectoryUser("ada@acme.com", "acme", Role.ADMIN))
    yield srv
    srv.stop()


def google_login(live: Live, google: FakeGoogle, email: str) -> tuple[httpx.Client, httpx.Response]:
    google.account = {**google.account, "email": email, "sub": f"g-{email}"}
    client = httpx.Client(base_url=live.url, follow_redirects=False)
    start = client.get("/admin/login/google")
    assert start.status_code == 302
    callback = httpx.get(start.headers["location"], follow_redirects=False)
    assert callback.headers["location"].startswith(f"{live.url}/admin/google/callback")
    return client, client.get(callback.headers["location"].removeprefix(live.url))


def test_google_sign_in_for_a_directory_admin(google_ui: Live, google: FakeGoogle) -> None:
    assert "Sign in with Google" in httpx.get(f"{google_ui.url}/admin/login").text
    client, done = google_login(google_ui, google, "ada@acme.com")
    assert done.status_code == 303 and done.headers["location"] == "/admin"
    assert "ada@acme.com" in client.get("/admin").text
    assert client.get("/admin/audit").status_code == 200
    google_ui.directory.upsert(DirectoryUser("ada@acme.com", "acme", Role.ADMIN, disabled=True))
    assert client.get("/admin").status_code == 303  # disabled users lose the session at once


def test_google_sign_in_refuses_strangers_and_replays(google_ui: Live, google: FakeGoogle) -> None:
    client, denied = google_login(google_ui, google, "mallory@acme.com")  # not in the directory
    assert denied.status_code == 403 and not client.cookies.get("hive_admin")
    client, ok = google_login(google_ui, google, "ada@acme.com")
    assert ok.status_code == 303
    replay = client.get(ok.request.url.raw_path.decode())  # the transaction is single use
    assert replay.status_code in (400, 403)


def test_google_callback_must_come_from_the_browser_that_started_it(
    google_ui: Live, google: FakeGoogle
) -> None:
    start = httpx.get(f"{google_ui.url}/admin/login/google", follow_redirects=False)
    callback = httpx.get(start.headers["location"], follow_redirects=False).headers["location"]
    stranger = httpx.get(callback, follow_redirects=False)  # no cookie: login CSRF
    assert stranger.status_code == 403


def test_a_different_google_account_behind_the_same_address_is_refused(
    google_ui: Live, google: FakeGoogle
) -> None:
    _, first = google_login(google_ui, google, "ada@acme.com")
    assert first.status_code == 303  # pins g-ada@acme.com
    assert google_ui.directory.lookup("ada@acme.com").google_sub == "g-ada@acme.com"
    google.account = {**google.account, "sub": "g-someone-else"}  # the address was reassigned
    client = httpx.Client(base_url=google_ui.url, follow_redirects=False)
    start = client.get("/admin/login/google")
    callback = httpx.get(start.headers["location"], follow_redirects=False)
    denied = client.get(callback.headers["location"].removeprefix(google_ui.url))
    assert denied.status_code == 403 and not client.cookies.get("hive_admin")


# ---------------------------------------------------------------- promotion history
async def test_history_lists_every_decision_newest_first_with_filters(ui: Live) -> None:
    first = await make_request(ui, title="First plan")
    second = await make_request(ui, title="Second plan")
    third = await make_request(ui, title="Third plan")
    client = sign_in(ui, APPROVER)
    token = csrf(client)
    decide = "/admin/promotions/decide"
    client.post(
        decide,
        data={"csrf": token, "promotion": first, "decision": "approve", "note": "looks good"},
    )
    client.post(
        decide,
        data={"csrf": token, "promotion": second, "decision": "reject", "note": "needs work"},
    )
    page = client.get("/admin/promotions/history").text
    assert (
        page.index("Third plan") < page.index("Second plan") < page.index("First plan")
    )  # newest first
    assert "looks good" in page and "needs work" in page and "tina" in page
    approved = client.get("/admin/promotions/history", params={"status": "approved"}).text
    assert (
        "First plan" in approved and "Second plan" not in approved and "Third plan" not in approved
    )
    pending = client.get("/admin/promotions/history", params={"status": "pending"}).text
    assert "Third plan" in pending and "First plan" not in pending
    assert client.get("/admin/promotions/history", params={"status": "<script>"}).status_code == 200
    assert third


async def test_history_is_limited_to_what_the_viewer_may_decide(ui: Live) -> None:
    await make_request(ui, title="Team one request")
    other = Principal(
        "olga", "org1", Role.MEMBER, approves=frozenset({Scope(ScopeLevel.TEAM, "t9")})
    )
    assert "Team one request" not in sign_in(ui, other).get("/admin/promotions/history").text
    assert (
        "Team one request" in sign_in(ui, ROOT).get("/admin/promotions/history").text
    )  # admins see all
    assert (
        sign_in(ui, BOB).get("/admin/promotions/history").status_code == 403
    )  # plain members do not


async def test_history_pages_and_survives_deleted_items(ui: Live, monkeypatch) -> None:
    from hive_mcp.webui import routes

    monkeypatch.setattr(routes, "HISTORY_PAGE", 2)  # the server runs in this process
    for n in range(3):
        await make_request(ui, title=f"Request {n}")
    client = sign_in(ui, ROOT)
    first = client.get("/admin/promotions/history").text
    assert (
        "Request 2" in first
        and "Request 1" in first
        and "Request 0" not in first
        and "Older" in first
    )
    second = client.get("/admin/promotions/history", params={"page": "2"}).text
    assert "Request 0" in second and "Newer" in second and "Older" not in second
    assert client.get("/admin/promotions/history", params={"page": "junk"}).status_code == 200
    assert "Request 2" in client.get("/admin/promotions/history", params={"page": "-4"}).text
