"""People and tokens managed from the admin page: the guards, the audit trail, and the pages."""

from __future__ import annotations

import re
from html import unescape

import httpx
import pytest

from hive_mcp.adapters.memory import MemoryBackend
from hive_mcp.adapters.memory.kv import MemoryKv
from hive_mcp.domain.clock import FixedClock
from hive_mcp.domain.errors import NotFound, PermissionDenied, ValidationError
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.security.directory import DirectoryUser, UserDirectory
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.security.tokens import TokenStore
from hive_mcp.webui.accounts import AccountAdmin, split
from hive_mcp.webui.session import SessionCodec, SessionStore
from tests.conformance.factory import ALICE
from tests.integration.conftest import Live

ADA = Principal("ada@acme.com", "acme", Role.ADMIN)


@pytest.fixture
def kit(tmp_path):
    backend = MemoryBackend()
    directory = UserDirectory(AtomicJsonFile(tmp_path / "users.json", default=[]))
    tokens = TokenStore(tmp_path / "tokens.json")
    sessions = SessionStore(SessionCodec(b"k" * 32), lambda: MemoryKv(FixedClock()))
    accounts = AccountAdmin(directory, tokens, sessions, lambda: backend.audit)
    directory.upsert(DirectoryUser("ada@acme.com", "acme", Role.ADMIN))
    directory.upsert(DirectoryUser("bob@acme.com", "acme", Role.MEMBER))
    directory.upsert(DirectoryUser("zed@other.com", "other", Role.ADMIN))
    return accounts, directory, tokens, sessions, backend


async def audited(backend, org="acme"):
    return [(r.event.action, r.event.target_id) for r in await backend.audit.list(org)]


def test_lists_are_split_on_commas_and_spaces():
    assert split("a, b  c,,d") == {"a", "b", "c", "d"} and split("") == frozenset()


# ---------------------------------------------------------------- people
async def test_adding_and_changing_a_person(kit):
    accounts, directory, _, _, backend = kit
    new = await accounts.save_user(
        ADA,
        " Cy@Acme.com ",
        "editor",
        "t1, t2",
        "p1",
        "team:t1",
    )
    assert new.email == "cy@acme.com" and new.org_id == "acme" and new.role is Role.EDITOR
    assert new.teams == {"t1", "t2"} and new.approves == {"team:t1"}
    assert directory.lookup("cy@acme.com") == new
    await accounts.save_user(ADA, "cy@acme.com", "member", "", "", "", disabled=True)
    assert directory.lookup("cy@acme.com") is None  # disabled
    assert ("admin.user.save", "cy@acme.com") in await audited(backend)


async def test_bad_input_is_refused_with_a_reason(kit):
    accounts, *_ = kit
    for args in (("not-an-email", "member"), ("cy@acme.com", "boss")):
        with pytest.raises(ValidationError):
            await accounts.save_user(ADA, args[0], args[1], "", "", "")
    with pytest.raises(ValidationError):
        await accounts.save_user(ADA, "cy@acme.com", "member", "", "", "planet:mars")
    with pytest.raises(ValidationError, match="another organisation"):
        await accounts.save_user(ADA, "zed@other.com", "member", "", "", "")


async def test_the_google_pin_is_kept_unless_reset(kit):
    accounts, directory, *_ = kit
    directory.upsert(DirectoryUser("bob@acme.com", "acme", Role.MEMBER, google_sub="g-1"))
    await accounts.save_user(ADA, "bob@acme.com", "editor", "", "", "")
    assert directory.lookup("bob@acme.com").google_sub == "g-1"
    await accounts.save_user(ADA, "bob@acme.com", "editor", "", "", "", reset_identity=True)
    assert directory.lookup("bob@acme.com").google_sub is None


async def test_nobody_can_lock_themselves_out(kit):
    accounts, directory, *_ = kit
    directory.upsert(DirectoryUser("eve@acme.com", "acme", Role.ADMIN))  # a second admin exists
    for call in (
        accounts.remove_user(ADA, "ada@acme.com"),
        accounts.set_disabled(ADA, "ada@acme.com", True),
        accounts.save_user(ADA, "ada@acme.com", "member", "", "", ""),
        accounts.save_user(ADA, "ada@acme.com", "admin", "", "", "", disabled=True),
    ):
        with pytest.raises(PermissionDenied, match="yourself"):
            await call
    saved = await accounts.save_user(
        ADA, "ada@acme.com", "admin", "t9", "", ""
    )  # harmless edits are fine
    assert saved.teams == {"t9"}


async def test_the_last_admin_cannot_be_removed_demoted_or_disabled(kit):
    accounts, directory, *_ = kit
    other = Principal("root", "acme", Role.ADMIN)  # a token session, not a directory person
    for call in (
        accounts.remove_user(other, "ada@acme.com"),
        accounts.set_disabled(other, "ada@acme.com", True),
        accounts.save_user(other, "ada@acme.com", "editor", "", "", ""),
    ):
        with pytest.raises(PermissionDenied, match="without an admin"):
            await call
    directory.upsert(DirectoryUser("eve@acme.com", "acme", Role.ADMIN))
    await accounts.remove_user(other, "ada@acme.com")  # now another admin remains
    directory.upsert(DirectoryUser("eve@acme.com", "acme", Role.ADMIN, disabled=True))
    with pytest.raises(NotFound):
        await accounts.remove_user(other, "ada@acme.com")


async def test_other_organisations_people_are_invisible_and_untouchable(kit):
    accounts, *_ = kit
    assert {u.email for u in accounts.users(ADA)} == {"ada@acme.com", "bob@acme.com"}
    for call in (
        accounts.remove_user(ADA, "zed@other.com"),
        accounts.set_disabled(ADA, "zed@other.com", True),
        accounts.sign_out_everywhere(ADA, "zed@other.com"),
    ):
        with pytest.raises(NotFound):
            await call


async def test_disabling_or_removing_ends_sessions_and_signout_everywhere_works(kit):
    accounts, directory, _, sessions, backend = kit
    first = await sessions.create("user", "bob@acme.com")
    second = await sessions.create("user", "bob@acme.com")
    await accounts.sign_out_everywhere(ADA, "bob@acme.com")
    assert await sessions.load(first) is None and await sessions.load(second) is None
    third = await sessions.create("user", "bob@acme.com")
    await accounts.set_disabled(ADA, "bob@acme.com", True)
    assert await sessions.load(third) is None
    await accounts.set_disabled(ADA, "bob@acme.com", False)
    assert directory.lookup("bob@acme.com") is not None
    fourth = await sessions.create("user", "bob@acme.com")
    await accounts.remove_user(ADA, "bob@acme.com")
    assert await sessions.load(fourth) is None
    actions = [a for a, _ in await audited(backend)]
    assert {
        "admin.user.signout",
        "admin.user.disable",
        "admin.user.enable",
        "admin.user.remove",
    } <= set(actions)


# ---------------------------------------------------------------- tokens
async def test_creating_listing_and_revoking_tokens(kit):
    accounts, _, tokens, _, backend = kit
    plain = await accounts.create_token(
        ADA, " ci ", "ci-pipeline", "member", "t1", "p1", "team:t1", "30"
    )
    assert plain.startswith("hive_") and tokens.find(plain).label == "ci"
    record = tokens.find(plain)
    assert (
        record.claims["hive_org"] == "acme" and record.expires_at is not None
    )  # the org is the admin's
    assert [t.label for t in accounts.tokens(ADA)] == ["ci"]
    tokens.create(Principal("x", "other", Role.MEMBER), label="theirs")
    assert [t.label for t in accounts.tokens(ADA)] == [
        "ci"
    ]  # other organisations' tokens are not shown
    with pytest.raises(NotFound):
        await accounts.revoke_token(ADA, "theirs")
    await accounts.revoke_token(ADA, "ci")
    assert tokens.find(plain) is None
    assert [a for a, _ in await audited(backend)] == ["admin.token.create", "admin.token.revoke"]


async def test_token_input_is_validated(kit):
    accounts, *_ = kit
    for subject, role, days in (
        ("", "member", ""),
        ("svc", "boss", ""),
        ("svc", "member", "abc"),
        ("svc", "member", "0"),
        ("svc", "member", "-3"),
        ("svc", "member", "99999"),
    ):
        with pytest.raises(ValidationError):
            await accounts.create_token(ADA, "t", subject, role, "", "", "", days)
    await accounts.create_token(ADA, "t", "svc", "member", "", "", "", "")
    with pytest.raises(ValidationError, match="unique"):
        await accounts.create_token(ADA, "t", "svc", "member", "", "", "", "")


# ---------------------------------------------------------------- the pages
@pytest.fixture
def ui(tmp_path):
    srv = Live(tmp_path / "data", admin_ui=True)
    srv.directory.upsert(DirectoryUser("ada@acme.com", "org1", Role.ADMIN))
    srv.directory.upsert(DirectoryUser("bob@acme.com", "org1", Role.MEMBER))
    srv.start()
    yield srv
    srv.stop()


def admin_client(live: Live) -> httpx.Client:
    client = httpx.Client(base_url=live.url, headers={"Origin": live.url})
    token = live.token_for(Principal("root", "org1", Role.ADMIN))
    assert client.post("/admin/login", data={"token": token}).status_code == 303
    return client


def csrf(client: httpx.Client, path: str) -> str:
    return unescape(re.search(r"name=csrf value='([^']+)'", client.get(path).text).group(1))


def test_only_admins_see_the_pages(ui):
    member = httpx.Client(base_url=ui.url, headers={"Origin": ui.url})
    member.post("/admin/login", data={"token": ui.token_for(ALICE)})
    for path in ("/admin/users", "/admin/tokens"):
        assert member.get(path).status_code == 403
    assert httpx.get(f"{ui.url}/admin/users", follow_redirects=False).status_code == 303


def test_people_page_end_to_end(ui):
    client = admin_client(ui)
    page = client.get("/admin/users").text
    assert "ada@acme.com" in page and "bob@acme.com" in page
    token = csrf(client, "/admin/users")
    saved = client.post(
        "/admin/users/save",
        data={
            "csrf": token,
            "email": "cy@acme.com",
            "role": "editor",
            "teams": "t1 t2",
            "projects": "",
            "approves": "team:t1",
        },
    )
    assert saved.status_code == 303 and saved.headers["location"] == "/admin/users?done=saved"
    assert (
        "cy@acme.com" in client.get("/admin/users").text
        and "Saved." in client.get(saved.headers["location"]).text
    )
    assert ui.directory.lookup("cy@acme.com").teams == {"t1", "t2"}
    bad = client.post(
        "/admin/users/save", data={"csrf": token, "email": "x@acme.com", "role": "boss"}
    )
    assert bad.status_code == 400 and "unknown role" in bad.text
    for action in ("disable", "enable", "signout", "remove"):
        r = client.post(f"/admin/users/{action}", data={"csrf": token, "email": "cy@acme.com"})
        assert r.status_code == 303, action
    assert ui.directory.lookup("cy@acme.com") is None
    missing = client.post("/admin/users/remove", data={"csrf": token, "email": "ghost@acme.com"})
    assert missing.status_code == 404
    forged = client.post("/admin/users/remove", data={"csrf": "x", "email": "bob@acme.com"})
    assert forged.status_code == 403 and ui.directory.lookup("bob@acme.com")
    cross = client.post(
        "/admin/users/remove",
        data={"csrf": token, "email": "bob@acme.com"},
        headers={"Origin": "https://evil.example"},
    )
    assert cross.status_code == 403 and ui.directory.lookup("bob@acme.com")


def test_the_last_admin_is_protected_on_the_page(ui):
    client = admin_client(ui)
    token = csrf(client, "/admin/users")
    r = client.post("/admin/users/remove", data={"csrf": token, "email": "ada@acme.com"})
    assert r.status_code == 403 and "without an admin" in r.text
    assert ui.directory.lookup("ada@acme.com")


def test_tokens_page_shows_a_new_token_once_and_it_works(ui):
    client = admin_client(ui)
    token = csrf(client, "/admin/tokens")
    created = client.post(
        "/admin/tokens/create",
        data={
            "csrf": token,
            "label": "robot",
            "subject": "robot",
            "role": "member",
            "teams": "",
            "projects": "p1",
            "approves": "",
            "days": "",
        },
    )
    assert created.status_code == 200 and "not shown again" in created.text
    assert created.headers["cache-control"] == "no-store"
    secret = re.search(r"<pre>(hive_[A-Za-z0-9_-]+)</pre>", created.text).group(1)
    assert secret not in client.get("/admin/tokens").text  # never listed again
    assert "robot" in client.get("/admin/tokens").text
    mcp = httpx.post(
        f"{ui.url}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Authorization": f"Bearer {secret}",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert mcp.status_code == 200
    dup = client.post(
        "/admin/tokens/create",
        data={"csrf": token, "label": "robot", "subject": "x", "role": "member"},
    )
    assert dup.status_code == 400 and "unique" in dup.text
    r = client.post("/admin/tokens/revoke", data={"csrf": token, "label": "robot"})
    assert r.status_code == 303
    again = httpx.post(
        f"{ui.url}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Authorization": f"Bearer {secret}",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert again.status_code == 401


def test_user_supplied_text_is_escaped_on_these_pages(ui):
    client = admin_client(ui)
    token = csrf(client, "/admin/users")
    evil = "<script>alert(1)</script>"
    ui.directory.upsert(DirectoryUser("x@acme.com", "org1", Role.MEMBER, teams=frozenset({evil})))
    ui.tokens.create(
        Principal("<b>s</b>", "org1", Role.MEMBER, teams=frozenset({evil})), label=evil
    )
    for path in ("/admin/users", "/admin/tokens"):
        assert (
            "<script>alert" not in client.get(path).text
            and "&lt;script&gt;" in client.get(path).text
        )
    bad = client.post("/admin/users/save", data={"csrf": token, "email": evil, "role": "member"})
    assert "<script>alert" not in bad.text
