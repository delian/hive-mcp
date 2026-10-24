import json
import stat
import time

import pytest

from hive_mcp.domain.errors import PermissionDenied, ValidationError
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.security.principals import principal_from_claims
from hive_mcp.security.tokens import TOKEN_PREFIX, StaticTokenVerifier, TokenStore, hash_token

ALICE = Principal("alice", "acme", Role.EDITOR, frozenset({"t1"}), frozenset({"p1", "p2"}))


def test_create_find_and_only_the_hash_is_stored(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    token = store.create(ALICE, label="ci")
    assert token.startswith(TOKEN_PREFIX) and len(token) > 40
    raw = (tmp_path / "tokens.json").read_text()
    assert token not in raw and hash_token(token) in raw
    assert stat.S_IMODE((tmp_path / "tokens.json").stat().st_mode) == 0o600
    record = store.find(token)
    assert record and record.subject == "alice" and record.claims["hive_projects"] == ["p1", "p2"]
    assert store.find(token + "x") is None and store.find("") is None


def test_labels_are_unique_and_required(tmp_path):
    store = TokenStore(tmp_path / "t.json")
    store.create(ALICE, label="a")
    for bad in ("a", ""):
        with pytest.raises(ValidationError):
            store.create(ALICE, label=bad)


def test_revoke_takes_effect_without_restart_and_is_reported(tmp_path):
    path = tmp_path / "t.json"
    token = TokenStore(path).create(ALICE, label="a")
    reader = TokenStore(path)  # a separate process / instance reading the same file
    assert reader.find(token)
    assert TokenStore(path).revoke("a") and not TokenStore(path).revoke("a")
    assert reader.find(token) is None


async def test_verifier_returns_claims_and_rejects_expired_unknown_and_foreign_prefixes(tmp_path):
    store = TokenStore(tmp_path / "t.json")
    verifier = StaticTokenVerifier(store)
    good = store.create(ALICE, label="good")
    access = await verifier.verify_token(good)
    assert access and access.subject == "alice" and access.claims["hive_org"] == "acme"
    expired = store.create(ALICE, label="old", expires_in_days=0.0001)
    data = json.loads((tmp_path / "t.json").read_text())
    for rec in data:
        if rec["label"] == "old":
            rec["expires_at"] = int(time.time()) - 5
    (tmp_path / "t.json").write_text(json.dumps(data))
    assert await verifier.verify_token(expired) is None
    assert await verifier.verify_token("hive_not-a-real-token") is None
    assert await verifier.verify_token("sk-" + "a" * 40) is None


def test_claims_round_trip_and_strictness():
    claims = {
        "hive_org": "acme",
        "hive_role": "editor",
        "hive_teams": ["t1"],
        "hive_projects": ["p1", "p2"],
    }
    assert principal_from_claims("alice", claims) == ALICE
    assert (
        principal_from_claims("x", {"hive_org": "acme"}).role is Role.VIEWER
    )  # absent role: read-only
    assert (
        principal_from_claims("x", {"hive_org": "acme", "hive_role": "root"}).role is Role.VIEWER
    )  # unknown too
    for subject, bad in [
        (None, claims),
        ("", claims),
        ("x", {}),
        ("x", {"hive_org": ""}),
        ("x", {**claims, "hive_projects": "p1"}),
        ("x", {**claims, "hive_teams": [1]}),
        ("x", {**claims, "hive_projects": ["a\x00b"]}),
        ("x", {**claims, "hive_projects": [str(i) for i in range(1001)]}),
    ]:
        with pytest.raises(PermissionDenied):
            principal_from_claims(subject, bad)


def test_revocation_is_never_missed_by_a_reader_even_in_a_tight_loop(tmp_path):
    path = tmp_path / "t.json"
    writer, reader = TokenStore(path), TokenStore(path)
    for i in range(
        150
    ):  # writes can share one timestamp tick; the reader must still see every change
        token = writer.create(ALICE, label=f"t{i}")
        assert reader.find(token) is not None
        assert writer.revoke(f"t{i}")
        assert reader.find(token) is None, f"revoked token still valid at iteration {i}"
