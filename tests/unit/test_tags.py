import pytest
from hypothesis import given
from hypothesis import strategies as st

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.tags import MAX_TAGS_PER_ITEM, normalize_tag, normalize_tags


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("RF", "rf"),
        ("  Domain/Radar  ", "domain/radar"),
        ("machine learning", "machine-learning"),
        ("snake_case_tag", "snake-case-tag"),
        ("Радар", "радар"),
        ("\uff32\uff26", "rf"),  # full-width forms fold to ASCII (NFKC)
        ("project//x/", "project/x"),
        ("v1.2", "v1.2"),
        ("tag\nnewline", "tag-newline"),  # whitespace of any kind is a word separator
    ],
)
def test_normalizes(raw, expected):
    assert normalize_tag(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "/",
        "../etc",
        "a b/../c",
        "x" * 65,
        "tag\x00nul",
        "tag\x1bescape",
        "emoji🙂",
        "-",
        "a--b/",
    ],
)
def test_rejects(bad):
    with pytest.raises(ValidationError):
        normalize_tag(bad)


@given(st.text(min_size=1, max_size=40))
def test_normalization_is_idempotent_when_valid(raw):
    try:
        once = normalize_tag(raw)
    except ValidationError:
        return
    assert normalize_tag(once) == once


def test_set_dedupes_and_limits():
    assert normalize_tags(["RF", "rf", " Rf "]) == frozenset({"rf"})
    with pytest.raises(ValidationError):
        normalize_tags([f"t{i}" for i in range(MAX_TAGS_PER_ITEM + 1)])
