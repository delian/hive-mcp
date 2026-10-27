"""Static bearer tokens: what is stored, how long they live, who may use them."""

from __future__ import annotations

import hashlib
import json
import os
import time

import pytest

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.security.principals import claims_for
from hive_mcp.security.tokens import (
    MAX_RATE_PER_MIN,
    TOKEN_PREFIX,
    StaticTokenVerifier,
    TokenStore,
    hash_token,
)

ANN = Principal("ann@acme.com", "acme", Role.MEMBER, projects=frozenset({"p1"}))


@pytest.fixture
def store(tmp_path):
    return TokenStore(tmp_path / "sub" / "dir" / "tokens.json")  # the directory does not exist yet


def test_a_token_is_stored_only_as_a_hash_with_the_claims_label_and_rate(store):
    token = store.create(ANN, label="ci", rate_per_min=5)
    assert (
        token.startswith(TOKEN_PREFIX) and len(token) == len(TOKEN_PREFIX) + 43
    )  # 32 random bytes
    raw = json.loads(store.path.read_text())
    assert token not in json.dumps(raw)
    [record] = store.list()
    assert record.token_hash == hashlib.sha256(token.encode()).hexdigest() == hash_token(token)
    assert record.subject == "ann@acme.com" and record.label == "ci" and record.rate_per_min == 5
    assert record.expires_at is None
    assert record.claims == claims_for(ANN, "ci") and record.claims["hive_org"] == "acme"


def test_labels_are_required_and_unique_with_a_clear_message(store):
    with pytest.raises(ValidationError) as caught:
        store.create(ANN, label="")
    assert str(caught.value) == "a unique label is required (it is how tokens are revoked)"
    store.create(ANN, label="one")
    with pytest.raises(ValidationError, match="unique label"):
        store.create(ANN, label="one")
    assert [r.label for r in store.list()] == ["one"]


def test_the_rate_limit_must_be_between_one_and_the_maximum_inclusive(store):
    for ok in (1, 2, MAX_RATE_PER_MIN):
        store.create(ANN, label=f"r{ok}", rate_per_min=ok)
    for bad in (0, -1, MAX_RATE_PER_MIN + 1):
        with pytest.raises(ValidationError) as caught:
            store.create(ANN, label=f"bad{bad}", rate_per_min=bad)
        assert str(caught.value) == f"rate per minute must be 1-{MAX_RATE_PER_MIN}"
    assert len(store.list()) == 3


def test_expiry_is_counted_in_days_from_now(store):
    before = time.time()
    store.create(ANN, label="week", expires_in_days=7)
    store.create(ANN, label="half", expires_in_days=0.5)
    store.create(ANN, label="never")
    records = {r.label: r for r in store.list()}
    assert records["week"].expires_at is not None
    assert abs(records["week"].expires_at - (before + 7 * 86_400)) < 5
    assert abs(records["half"].expires_at - (before + 43_200)) < 5
    assert records["never"].expires_at is None


def test_revoking_and_finding_and_the_rate_lookup(store):
    a = store.create(ANN, label="a", rate_per_min=7)
    b = store.create(ANN, label="b")
    assert store.find(a).label == "a" and store.find(b).label == "b" and store.find("nope") is None
    key = "tok:" + hash_token(a)[:32]
    assert store.rate_for_key(key) == 7 and store.rate_for_key("tok:" + hash_token(b)[:32]) is None
    assert store.rate_for_key("tok:zzz") is None
    assert store.revoke("a") is True and store.revoke("a") is False
    assert store.find(a) is None and store.rate_for_key(key) is None


class Directory:
    def __init__(self, allowed: set[str]) -> None:
        self.allowed, self.asked = allowed, []

    def permits(self, subject: str) -> bool:
        self.asked.append(subject)
        return subject in self.allowed

    def __bool__(self) -> bool:
        return True


async def test_the_verifier_checks_prefix_expiry_and_the_directory(store):
    token = store.create(ANN, label="ci")
    expiring = store.create(ANN, label="exp", expires_in_days=1)
    verifier = StaticTokenVerifier(store, resource="https://hive.example/mcp")
    got = await verifier.verify_token(token)
    assert got is not None
    assert got.token == token and got.client_id == "static:ci" and got.scopes == ["hive"]
    assert got.subject == "ann@acme.com" and got.resource == "https://hive.example/mcp"
    assert got.expires_at is None and got.claims == claims_for(ANN, "ci")
    soon = await verifier.verify_token(expiring)
    assert soon is not None and soon.expires_at == store.find(expiring).expires_at
    assert await verifier.verify_token("not-ours") is None
    assert await verifier.verify_token(token[len(TOKEN_PREFIX) :]) is None  # prefix is required


async def test_an_expired_token_and_a_removed_person_are_refused_at_the_boundary(
    store, monkeypatch
):
    token = store.create(ANN, label="ci", expires_in_days=1)
    record = store.find(token)
    verifier = StaticTokenVerifier(store)
    monkeypatch.setattr(time, "time", lambda: float(record.expires_at) - 1)
    assert await verifier.verify_token(token) is not None
    monkeypatch.setattr(time, "time", lambda: float(record.expires_at))
    assert await verifier.verify_token(token) is None  # exactly at expiry: gone
    monkeypatch.undo()
    allowed, removed = Directory({"ann@acme.com"}), Directory(set())
    assert await StaticTokenVerifier(store, directory=allowed).verify_token(token) is not None
    assert allowed.asked == ["ann@acme.com"]
    assert await StaticTokenVerifier(store, directory=removed).verify_token(token) is None
    assert removed.asked == ["ann@acme.com"]


def test_the_token_file_is_created_with_its_parent_directories(store, tmp_path):
    store.create(ANN, label="x")
    assert (tmp_path / "sub" / "dir" / "tokens.json").is_file()
    assert os.path.isdir(tmp_path / "sub" / "dir")
    assert TokenStore(tmp_path / "sub" / "dir" / "tokens.json").path.name == "tokens.json"


def test_creating_a_token_without_a_label_is_refused(store):
    with pytest.raises(ValidationError, match="unique label"):
        store.create(ANN)
