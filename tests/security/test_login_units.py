import stat

import pytest

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.principal import Role
from hive_mcp.security.directory import DirectoryUser, UserDirectory, normalize_email
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.security.oauth.cookies import CookieSigner, load_secret_key
from hive_mcp.security.oauth.login import Denied, check_identity
from hive_mcp.security.oauth.pages import SECURITY_HEADERS, consent_page, error_page
from hive_mcp.security.oauth.upstream import (
    GoogleIdentity,
    GoogleOidc,
    UpstreamError,
    pkce_challenge,
)


@pytest.fixture
def directory(tmp_path):
    d = UserDirectory(AtomicJsonFile(tmp_path / "users.json", default=[]))
    d.upsert(
        DirectoryUser("alice@acme.com", "acme", Role.MEMBER, frozenset({"t1"}), frozenset({"p1"}))
    )
    d.upsert(DirectoryUser("off@acme.com", "acme", Role.MEMBER, disabled=True))
    d.upsert(DirectoryUser("carol@other.org", "other", Role.MEMBER))
    return d


def ident(email="alice@acme.com", verified=True, hd="acme.com"):
    return GoogleIdentity(email=email, email_verified=verified, hosted_domain=hd, subject="g-1")


# ---------------------------------------------------------------- who may sign in
def test_a_listed_verified_workspace_user_may_sign_in(directory):
    user = check_identity(ident(), ["acme.com"], directory)
    assert user.email == "alice@acme.com" and user.principal().projects == {"p1"}
    assert (
        check_identity(ident("ALICE@Acme.COM"), ["ACME.com"], directory).email == "alice@acme.com"
    )


@pytest.mark.parametrize(
    ("identity", "allowed", "reason"),
    [
        (ident(verified=False), ["acme.com"], "not verified"),
        (ident(hd=None), ["acme.com"], "Workspace"),  # personal account using the same address
        (ident(hd="evil.com"), ["acme.com"], "Workspace"),
        (ident("alice@evil.com", hd="evil.com"), ["acme.com"], "not allowed"),
        (ident("stranger@acme.com"), ["acme.com"], "directory"),
        (ident("off@acme.com"), ["acme.com"], "directory"),  # disabled
        (ident("carol@other.org", hd="other.org"), ["acme.com"], "not allowed"),
        (ident(), [], "not allowed"),  # nothing configured: nobody gets in
        (ident("not-an-email"), ["acme.com"], "malformed"),
        (ident("a@b@acme.com"), ["acme.com"], "malformed"),
        (ident("alice@acme.com\x00@evil.com"), ["acme.com"], "malformed"),
    ],
)
def test_everyone_else_is_refused(directory, identity, allowed, reason):
    outcome = check_identity(identity, allowed, directory)
    assert isinstance(outcome, Denied) and reason in outcome.reason


def test_lookalike_domains_do_not_match(directory):
    for email in ("alice@acme.com.evil.com", "alice@evilacme.com", "alice@sub.acme.com"):
        assert isinstance(
            check_identity(ident(email, hd="acme.com"), ["acme.com"], directory), Denied
        )


# ---------------------------------------------------------------- directory
def test_directory_crud_normalisation_and_live_edits(tmp_path):
    path = tmp_path / "users.json"
    d1, d2 = (
        UserDirectory(AtomicJsonFile(path, default=[])),
        UserDirectory(AtomicJsonFile(path, default=[])),
    )
    d1.upsert(DirectoryUser(" Dave@Acme.com ", "acme"))  # type: ignore[arg-type]
    assert d2.lookup("dave@acme.com") is not None  # another process sees the edit at once
    assert (
        d1.remove("DAVE@acme.com")
        and d2.lookup("dave@acme.com") is None
        and not d1.remove("dave@acme.com")
    )
    assert d2.lookup("garbage") is None and d2.lookup("") is None
    assert path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "bad", ["", "a", "a@", "@b.com", "a b@c.com", "a@b@c.com", "x" * 300 + "@a.com"]
)
def test_bad_emails_are_rejected(bad):
    with pytest.raises(ValidationError):
        normalize_email(bad)


# ---------------------------------------------------------------- cookie binding
def test_cookie_only_validates_for_its_own_transaction():
    signer, other = CookieSigner(b"k" * 32), CookieSigner(b"j" * 32)
    cookie = signer.sign("txn-1")
    assert signer.verify(cookie, "txn-1")
    assert not signer.verify(cookie, "txn-2")  # a cookie from a different login
    assert not other.verify(cookie, "txn-1")  # forged with another key
    for bad in (None, "", "txn-1", "txn-1.", "txn-1.zz", cookie + "0", cookie[:-1]):
        assert not signer.verify(bad, "txn-1")
    assert not signer.verify(signer.sign(""), "")


def test_secret_key_is_generated_once_and_kept_private(tmp_path):
    path = tmp_path / "sub" / "secret.key"
    key = load_secret_key(None, path)
    assert len(key) == 32 and load_secret_key(None, path) == key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_secret_key("configured", path) == b"configured"


# ---------------------------------------------------------------- pages
def test_untrusted_text_is_escaped_and_pages_cannot_be_framed_or_cached():
    evil = "<script>alert(1)</script>\"'&"
    response = consent_page(
        app_name=evil,
        redirect_host='https://x.example/"><b>',
        email="a@b.c<i>",
        scopes=["hive"],
        txn="t'\"<",
        csrf="c'\"<",
    )
    html = response.body.decode()
    # The page uses its own <b>/<code> tags, so check the *injected* markup: only ever escaped.
    assert "<script>" not in html and "<i>" not in html and '"><b>' not in html
    assert "&lt;script&gt;" in html and "&lt;i&gt;" in html and "&quot;&gt;&lt;b&gt;" in html
    assert "t&#x27;&quot;&lt;" in html
    assert (
        response.headers["x-frame-options"] == "DENY"
        and response.headers["cache-control"] == "no-store"
    )
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert (
        "script-src" not in SECURITY_HEADERS["Content-Security-Policy"]
    )  # no scripts allowed at all
    assert "<script>" not in error_page("<script>x</script>").body.decode()


# ---------------------------------------------------------------- upstream
def test_upstream_issuer_must_use_https_except_loopback():
    def make(issuer):
        return GoogleOidc(
            client_id="c", client_secret="s", issuer=issuer, callback_url="https://x/cb"
        )

    make("https://accounts.google.com")
    make("http://127.0.0.1:9000")
    make("http://localhost:9000")
    for bad in ("http://accounts.google.com", "http://evil.example", "ftp://x"):
        with pytest.raises(UpstreamError):
            make(bad)


def test_pkce_challenge_matches_the_rfc_example():
    assert (
        pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )
