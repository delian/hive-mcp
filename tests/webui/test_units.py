"""Pieces of the admin UI that are awkward to reach over HTTP: expiry and escaping."""

import time

from hive_mcp.security.directory import UserDirectory
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.security.tokens import TokenStore
from hive_mcp.webui import views
from hive_mcp.webui.auth import AdminAuth
from hive_mcp.webui.layout import table
from hive_mcp.webui.session import SESSION_SECONDS, SessionCodec
from tests.conformance.factory import ROOT


def test_sessions_expire_exactly_at_their_time_and_not_before():
    codec = SessionCodec(b"k" * 32)
    cookie = codec.issue("token", "ci", now=1000.0)
    assert codec.read(cookie, now=1000.0 + SESSION_SECONDS - 1) is not None
    assert codec.read(cookie, now=1000.0 + SESSION_SECONDS) is None


def test_sessions_signed_with_another_key_or_purpose_are_refused():
    cookie = SessionCodec(b"a" * 32).issue("token", "ci")
    assert SessionCodec(b"b" * 32).read(cookie) is None
    assert SessionCodec(b"a" * 32).read(cookie) is not None


def test_a_token_that_expires_after_sign_in_stops_working(tmp_path, monkeypatch):
    store = TokenStore(tmp_path / "t.json")
    auth = AdminAuth(UserDirectory(AtomicJsonFile(tmp_path / "u.json", default=[])), store)
    token = store.create(ROOT, label="soon", expires_in_days=1)
    assert auth.principal_for_label("soon") is not None and auth.token_label(token) == "soon"
    later = time.time() + 2 * 86_400
    monkeypatch.setattr("hive_mcp.webui.auth.time.time", lambda: later)
    assert auth.principal_for_label("soon") is None
    assert auth.token_label(token) is None


def test_table_headers_are_escaped():
    assert "<script>" not in table(["<script>"], [["x"]])


def test_login_notice_is_escaped():
    assert "<b>" not in views.login_form(google=False, notice="<b>hi</b>")
