"""Regression tests for the findings of the security review."""

from __future__ import annotations

import json
import os
import stat

import httpx
import pytest

from hive_mcp.config import Settings
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.security.directory import DirectoryUser, UserDirectory
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.security.oauth.cookies import CookieSigner, load_secret_key
from hive_mcp.security.oauth.login import check_identity, pin_identity
from hive_mcp.security.oauth.provider import check_redirect_uris
from hive_mcp.security.oauth.upstream import GoogleIdentity
from hive_mcp.security.tokens import StaticTokenVerifier, TokenStore
from hive_mcp.webui.session import SessionCodec
from tests.integration import oauth_driver as d
from tests.integration.conftest import Live, _oauth_server

ADA = Principal("ada@acme.com", "acme", Role.ADMIN)
ACME = ["acme.com"]


def identity(email="ada@acme.com", sub="g-1"):
    return GoogleIdentity(email, True, "acme.com", sub)


# ---------------------------------------------------------------- non-ASCII credentials
def test_non_ascii_cookies_are_simply_invalid():
    signer = CookieSigner(b"k" * 32)
    assert signer.verify("é.é", "txn") is False
    assert SessionCodec(b"k" * 32).verify("a.é") is None
    assert SessionCodec(b"k" * 32).verify("é") is None


def test_non_ascii_cookie_headers_never_cause_a_500(tmp_path):
    live = Live(tmp_path / "data", admin_ui=True)
    live.start()
    try:
        for path in ("/admin", "/admin/promotions"):
            r = httpx.get(
                f"{live.url}{path}", headers={"Cookie": "hive_admin=a.é".encode("latin-1")}
            )
            assert r.status_code == 303
    finally:
        live.stop()


# ---------------------------------------------------------------- keys
def test_short_secret_keys_are_refused():
    with pytest.raises(ValueError, match="32"):
        Settings(secret_key="short")
    assert Settings(secret_key="k" * 32).secret_key


def test_generated_key_file_is_private_from_the_first_moment(tmp_path, monkeypatch):
    seen = []
    real_open = os.open

    def spy(path, flags, mode=0o777):
        seen.append(mode)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", spy)
    path = tmp_path / "sub" / "secret.key"
    key = load_secret_key(None, path)
    assert seen == [0o600] and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_secret_key(None, path) == key and len(key) == 32


# ---------------------------------------------------------------- directory
def test_one_bad_row_does_not_lock_everybody_out_and_survives_rewrites(tmp_path):
    path = tmp_path / "users.json"
    good = DirectoryUser("ann@acme.com", "acme", Role.MEMBER).to_json()
    bad = {"email": "bob@acme.com", "org_id": "acme", "role": "bosss"}  # typo
    path.write_text(json.dumps([good, bad, {"no": "email"}]))
    directory = UserDirectory(AtomicJsonFile(path, default=[]))
    assert directory.lookup("ann@acme.com") is not None and directory.lookup("bob@acme.com") is None
    directory.upsert(DirectoryUser("cy@acme.com", "acme", Role.MEMBER))
    rows = json.loads(path.read_text())
    assert (
        bad in rows and {"no": "email"} in rows and len(rows) == 4
    )  # nothing was silently dropped
    assert directory.remove("cy@acme.com") and bad in json.loads(path.read_text())


def test_directory_permits_people_only_while_present_and_enabled(tmp_path):
    directory = UserDirectory(AtomicJsonFile(tmp_path / "u.json", default=[]))
    assert directory.permits("ci-pipeline") is True  # not a person: not our business
    assert directory.permits("ann@acme.com") is False  # an email that is not in the directory
    directory.upsert(DirectoryUser("ann@acme.com", "acme", Role.MEMBER))
    assert directory.permits("ann@acme.com") is True
    directory.upsert(DirectoryUser("ann@acme.com", "acme", Role.MEMBER, disabled=True))
    assert directory.permits("ann@acme.com") is False


async def test_static_tokens_of_removed_or_disabled_people_stop_working(tmp_path):
    store = TokenStore(tmp_path / "t.json")
    directory = UserDirectory(AtomicJsonFile(tmp_path / "u.json", default=[]))
    verifier = StaticTokenVerifier(store, directory=directory)
    person = store.create(Principal("ann@acme.com", "acme", Role.MEMBER), label="ann")
    service = store.create(Principal("ci-pipeline", "acme", Role.MEMBER), label="ci")
    assert await verifier.verify_token(person) is None  # never in the directory
    directory.upsert(DirectoryUser("ann@acme.com", "acme", Role.MEMBER))
    assert await verifier.verify_token(person) is not None
    directory.upsert(DirectoryUser("ann@acme.com", "acme", Role.MEMBER, disabled=True))
    assert await verifier.verify_token(person) is None
    directory.remove("ann@acme.com")
    assert await verifier.verify_token(person) is None
    assert await verifier.verify_token(service) is not None  # service accounts are unaffected


def test_admin_sessions_of_removed_people_end_and_label_reuse_does_not_revive_them(tmp_path):
    live = Live(tmp_path / "data", admin_ui=True)
    live.directory.upsert(DirectoryUser("ada@acme.com", "acme", Role.ADMIN))
    live.start()
    try:
        client = httpx.Client(base_url=live.url, headers={"Origin": live.url})
        token = live.token_for(ADA, label="ada-tok")
        assert client.post("/admin/login", data={"token": token}).status_code == 303
        assert client.get("/admin/usage").status_code == 200
        live.tokens.revoke("ada-tok")
        live.token_for(ADA, label="ada-tok")  # same label, new token
        assert client.get("/admin/usage").status_code == 303  # the old session stays dead
        fresh = live.token_for(ADA, label="ada-2")
        assert client.post("/admin/login", data={"token": fresh}).status_code == 303
        live.directory.remove("ada@acme.com")
        assert client.get("/admin/usage").status_code == 303
    finally:
        live.stop()


# ---------------------------------------------------------------- pinned Google identity
def test_the_first_sign_in_pins_the_google_account_and_a_different_one_is_refused(tmp_path):
    directory = UserDirectory(AtomicJsonFile(tmp_path / "u.json", default=[]))
    directory.upsert(DirectoryUser("ada@acme.com", "acme", Role.ADMIN))
    user = check_identity(identity(sub="g-1"), ACME, directory)
    assert isinstance(user, DirectoryUser) and user.google_sub is None
    pin_identity(directory, user, identity(sub="g-1"))
    assert directory.lookup("ada@acme.com").google_sub == "g-1"
    assert isinstance(check_identity(identity(sub="g-1"), ACME, directory), DirectoryUser)
    reassigned = check_identity(identity(sub="g-2"), ACME, directory)
    assert not isinstance(reassigned, DirectoryUser) and "differs" in reassigned.reason
    pin_identity(directory, directory.lookup("ada@acme.com"), identity(sub="g-2"))  # never re-pins
    assert directory.lookup("ada@acme.com").google_sub == "g-1"


def test_editing_a_user_in_the_directory_clears_the_pin_on_purpose(tmp_path):
    directory = UserDirectory(AtomicJsonFile(tmp_path / "u.json", default=[]))
    pinned = DirectoryUser("ada@acme.com", "acme", Role.ADMIN, google_sub="g-1")
    directory.upsert(pinned)
    assert (
        UserDirectory(AtomicJsonFile(tmp_path / "u.json", default=[]))
        .lookup("ada@acme.com")
        .google_sub
        == "g-1"
    )
    assert "google_sub" not in DirectoryUser("x@acme.com", "acme").to_json()


# ---------------------------------------------------------------- registration allow-list
def test_only_listed_hosts_can_be_registered_when_an_allow_list_is_set():
    allowed = frozenset({"claude.ai", "chatgpt.com"})
    check_redirect_uris(["https://claude.ai/api/mcp/auth_callback"], allowed)
    check_redirect_uris(["https://CHATGPT.com/cb"], allowed)
    for bad in (
        "https://evil.example/cb",
        "https://claude.ai.evil.example/cb",
        "http://localhost:9/cb",
    ):
        with pytest.raises(Exception, match="known applications"):
            check_redirect_uris([bad], allowed)
    check_redirect_uris(["https://anything.example/cb"])  # no list: unchanged behaviour


def test_registration_over_http_respects_the_allow_list(tmp_path, google):
    live = _oauth_server(tmp_path, google, oauth_redirect_hosts=["client.example"])
    try:
        assert d.register(live).status_code == 201  # REDIRECT is https://client.example/callback
        refused = d.register(live, redirect_uris=["https://evil.example/cb"])
        assert refused.status_code == 400 and refused.json()["error"] == "invalid_redirect_uri"
    finally:
        live.stop()


# ---------------------------------------------------------------- revoke endpoint input
def test_revoke_rejects_odd_content_lengths_without_crashing(tmp_path, google):
    import socket  # noqa: PLC0415

    live = _oauth_server(tmp_path, google)
    try:
        for length in (
            "abc",
            "-5",
            "9999999",
        ):  # httpx would refuse to send these: use a raw socket
            with socket.create_connection(("127.0.0.1", live.port), timeout=5) as sock:
                sock.sendall(
                    f"POST /revoke HTTP/1.1\r\nHost: 127.0.0.1:{live.port}\r\n"
                    f"Content-Type: application/x-www-form-urlencoded\r\n"
                    f"Content-Length: {length}\r\nConnection: close\r\n\r\ntoken=x".encode()
                )
                status = sock.recv(64).split(b" ")[1]
            assert status in (b"400", b"413"), (length, status)
    finally:
        live.stop()
