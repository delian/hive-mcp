"""The single tag normaliser: tools, CLI, ingest and import must all go through it."""

from __future__ import annotations

import re
import unicodedata

from hive_mcp.domain.errors import ValidationError

MAX_TAG_LEN = 64
MAX_TAGS_PER_ITEM = 32
_SEPARATORS = re.compile(r"[\s_]+")
_VALID = re.compile(r"^[^\W_]+(?:[-.][^\W_]+)*(?:/[^\W_]+(?:[-.][^\W_]+)*)*$")


def normalize_tag(raw: str) -> str:
    """Canonical form: NFKC, lower-case, `facet/value`, words joined by '-'.

    Unicode letters are allowed (non-English teams); path-like or control characters are not.
    """
    text = unicodedata.normalize("NFKC", raw).strip().casefold()
    text = _SEPARATORS.sub("-", text).strip("-/")
    text = re.sub(r"/{2,}", "/", text)
    if not text or len(text) > MAX_TAG_LEN or not _VALID.match(text):
        raise ValidationError(f"invalid tag: {raw!r}")
    return text


def normalize_tags(raw: list[str] | tuple[str, ...] | frozenset[str]) -> frozenset[str]:
    tags = frozenset(normalize_tag(t) for t in raw)
    if len(tags) > MAX_TAGS_PER_ITEM:
        raise ValidationError(f"too many tags (max {MAX_TAGS_PER_ITEM})")
    return tags
