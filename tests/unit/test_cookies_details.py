"""The login-binding cookie and the key that signs it."""

from __future__ import annotations

import stat

from hive_mcp.security.oauth.cookies import COOKIE_NAME, CookieSigner, load_secret_key


def test_a_configured_key_is_used_as_is_and_nothing_is_written(tmp_path):
    path = tmp_path / "x" / "secret.key"
    assert load_secret_key("configured-key", path) == b"configured-key"
    assert not path.exists() and not path.parent.exists()


def test_a_generated_key_is_kept_owner_only_and_reused(tmp_path):
    path = tmp_path / "deep" / "dir" / "secret.key"  # parents are created
    key = load_secret_key(None, path)
    assert len(key) == 32 and path.read_text() == key.hex()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_secret_key(None, path) == key  # the same key next time
    assert load_secret_key("", path) == key  # an empty setting counts as none
    path.write_text(f"  {key.hex()}\n")  # whitespace around the stored key is ignored
    assert load_secret_key(None, path) == key
    assert (
        load_secret_key(None, tmp_path / "other" / "secret.key") != key
    )  # another place, another key


def test_the_cookie_belongs_to_one_transaction_and_one_key():
    signer = CookieSigner(b"k" * 32)
    cookie = signer.sign("txn-1")
    assert cookie.startswith("txn-1.") and len(cookie.split(".")[1]) == 64
    assert signer.verify(cookie, "txn-1") is True
    assert signer.verify(cookie, "txn-2") is False
    assert signer.verify(None, "txn-1") is False and signer.verify("", "txn-1") is False
    assert signer.verify(cookie, "") is False
    assert CookieSigner(b"j" * 32).verify(cookie, "txn-1") is False
    assert signer.verify(cookie + "0", "txn-1") is False
    assert (
        signer.verify("é" + cookie, "txn-1") is False
    )  # non-ASCII is a plain refusal, not a crash
    assert signer.sign("txn-1") == cookie and signer.sign("txn-2") != cookie
    assert COOKIE_NAME == "hive_oauth"


def test_the_mac_is_bound_to_the_transaction_so_a_cookie_cannot_be_relabelled():
    signer = CookieSigner(b"k" * 32)
    mac = signer.sign("txn-1").split(".", 1)[1]
    assert signer.verify(f"txn-2.{mac}", "txn-2") is False  # the mac of one transaction, relabelled
    assert signer.sign("txn-2").split(".", 1)[1] != mac


def test_a_key_file_can_be_created_in_a_directory_that_already_exists(tmp_path):
    key = load_secret_key(None, tmp_path / "secret.key")  # the parent exists: that is fine
    assert len(key) == 32
