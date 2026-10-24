import base64
from datetime import UTC, datetime, timedelta

import pytest

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.item import (
    Cursor,
    Item,
    ItemPatch,
    ItemQuery,
    Kind,
    Status,
    apply_patch,
    content_hash,
)
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def item(**kw):
    base = dict(
        id="i1",
        org_id="o",
        kind=Kind.NOTE,
        title="T",
        body="B",
        scope=Scope(ScopeLevel.PROJECT, "p"),
        author_id="a",
        created_at=NOW,
        updated_at=NOW,
    )
    return Item(**{**base, **kw})


@pytest.mark.parametrize(
    "bad",
    [
        {"title": " "},
        {"title": "x" * 201},
        {"body": ""},
        {"revision": 0},
        {"id": ""},
        {"tags": frozenset({"Not Normalised"})},
        {"created_at": datetime(2026, 1, 1)},  # naive timestamp
        {"scope": Scope(ScopeLevel.PRIVATE, "a"), "visibility": Visibility.ORG},
    ],
)
def test_item_invariants(bad):
    with pytest.raises(ValidationError):
        item(**bad)


def test_content_hash_is_unicode_normalisation_stable():
    composed, decomposed = "café", "café"
    assert content_hash(Kind.NOTE, composed, "x") == content_hash(Kind.NOTE, decomposed, "x")
    assert content_hash(Kind.NOTE, "a", "b") != content_hash(Kind.FORMULA, "a", "b")


def test_fingerprint_covers_tags_and_scope_but_hash_does_not():
    a, b = item(), item(tags=frozenset({"x"}))
    assert a.content_hash == b.content_hash and a.fingerprint() != b.fingerprint()


def test_patch_rules():
    with pytest.raises(ValidationError):
        ItemPatch()
    with pytest.raises(ValidationError):
        ItemPatch(add_tags=frozenset({"x"}), remove_tags=frozenset({"x"}))
    with pytest.raises(ValidationError):
        ItemPatch(add_tags=frozenset({"Bad Tag"}))


def test_apply_patch_bumps_revision_and_revalidates():
    later = NOW + timedelta(hours=1)
    patched = apply_patch(
        item(tags=frozenset({"a"})), ItemPatch(title="N", add_tags=frozenset({"b"})), later
    )
    assert (patched.revision, patched.title, patched.tags, patched.updated_at) == (
        2,
        "N",
        {"a", "b"},
        later,
    )
    assert patched.created_at == NOW
    with pytest.raises(ValidationError):
        apply_patch(item(), ItemPatch(title=" "), later)  # a patch cannot smuggle in a blank title


def test_cursor_roundtrip_and_garbage():
    c = Cursor(NOW, "id-1")
    assert Cursor.decode(c.encode()) == c
    naive = base64.urlsafe_b64encode(b'["2026-01-01T00:00:00", "x"]').decode()
    for junk in ("", "not-base64!!", "e30=", "WzEsMl0=", naive):
        with pytest.raises(ValidationError):
            Cursor.decode(junk)


def test_query_limits():
    for bad in (0, 101):
        with pytest.raises(ValidationError):
            ItemQuery(limit=bad)
    assert Status.ARCHIVED not in ItemQuery().statuses
