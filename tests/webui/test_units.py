"""Pieces of the admin UI that are awkward to reach over HTTP: expiry and escaping."""

import time

from hive_mcp.adapters.memory.kv import MemoryKv
from hive_mcp.domain.clock import FixedClock
from hive_mcp.security.directory import UserDirectory
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.security.oauth.cookies import CookieSigner
from hive_mcp.security.tokens import TokenStore
from hive_mcp.webui import views
from hive_mcp.webui.auth import AdminAuth
from hive_mcp.webui.layout import table
from hive_mcp.webui.session import NS_SESSION, SESSION_SECONDS, SessionCodec, SessionStore
from tests.conformance.factory import ROOT


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_store(key=b"k" * 32, kv=None, clock=None):
    kv = kv or MemoryKv(FixedClock())
    return SessionStore(SessionCodec(key), lambda: kv, clock or Clock()), kv


async def test_sessions_expire_exactly_at_their_time_and_not_before():
    clock = Clock()
    store, _ = make_store(clock=clock)
    cookie = await store.create("token", "ci")
    clock.t += SESSION_SECONDS - 1
    assert await store.load(cookie) is not None
    clock.t += 1
    assert await store.load(cookie) is None


async def test_cookies_are_signed_per_key_and_garbage_never_reaches_the_store():
    store, kv = make_store()
    cookie = await store.create("token", "ci")
    other, _ = make_store(key=b"b" * 32, kv=kv)
    assert await other.load(cookie) is None and await store.load(cookie) is not None
    for junk in (None, "", "x", "x.y", cookie + "0", "é.é", cookie.split(".")[0]):
        assert await store.load(junk) is None


async def test_the_store_never_holds_the_session_id_that_is_in_the_cookie():
    kv = MemoryKv(FixedClock())
    store, _ = make_store(kv=kv)
    cookie = await store.create("user", "ann@acme.com")
    session_id = cookie.split(".")[0]
    assert all(session_id not in key for (_, key) in kv._data) if hasattr(kv, "_data") else True
    assert await kv.get(NS_SESSION, session_id) is None  # only its hash is a key


async def test_signing_out_destroys_the_session_even_for_a_copied_cookie():
    store, _ = make_store()
    cookie = await store.create("user", "ann@acme.com")
    copy = cookie
    await store.destroy(cookie)
    assert await store.load(copy) is None
    await store.destroy("junk")  # harmless


async def test_ending_a_subjects_sessions_leaves_later_and_other_ones_alone():
    store, _ = make_store()
    old_a = await store.create("user", "ann@acme.com")
    old_b = await store.create("user", "ann@acme.com")
    bob = await store.create("user", "bob@acme.com")
    await store.revoke_subject("ann@acme.com")
    assert await store.load(old_a) is None and await store.load(old_b) is None
    assert await store.load(bob) is not None
    fresh = await store.create("user", "ann@acme.com")  # signing in again works
    assert await store.load(fresh) is not None
    await store.revoke_subject("ann@acme.com")
    assert await store.load(fresh) is None


def test_session_cookies_of_another_purpose_are_not_accepted():
    codec = SessionCodec(b"k" * 32)
    assert (
        codec.verify(CookieSigner(b"k" * 32).sign("abc")) is None
    )  # the OAuth login cookie format


def test_a_token_that_expires_after_sign_in_stops_working(tmp_path, monkeypatch):
    store = TokenStore(tmp_path / "t.json")
    auth = AdminAuth(UserDirectory(AtomicJsonFile(tmp_path / "u.json", default=[])), store)
    token = store.create(ROOT, label="soon", expires_in_days=1)
    subject = auth.token_label(token)
    assert subject and auth.principal_for_label(subject) is not None
    later = time.time() + 2 * 86_400
    monkeypatch.setattr("hive_mcp.webui.auth.time.time", lambda: later)
    assert auth.principal_for_label(subject) is None
    assert auth.token_label(token) is None


def test_table_headers_are_escaped():
    assert "<script>" not in table(["<script>"], [["x"]])


def test_login_notice_is_escaped():
    assert "<b>" not in views.login_form(google=False, notice="<b>hi</b>")
