"""Entities: people, systems, parts and concepts that several notes talk about.

An entity is an ordinary item (kind=entity) so it has scope, visibility, history and links like
any other. Its name and every alias are stored as `alias/<slug>` tags, which makes "is this thing
already known?" an exact tag lookup, and `entity/<type>` says what kind of thing it is.
"""

from __future__ import annotations

import re
import unicodedata

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.tags import MAX_TAG_LEN

ALIAS_PREFIX = "alias/"
TYPE_PREFIX = "entity/"
MAX_ALIASES = 20
MAX_SLUG = MAX_TAG_LEN - len(ALIAS_PREFIX)
_SYMBOLS = {"+": " plus ", "#": " sharp ", "&": " and ", "@": " at "}
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def entity_slug(name: str) -> str:
    """Normal form for comparing names: case, accents-as-written, punctuation and spacing ignored.
    "Pulse-Doppler  radar", "pulse doppler radar" and "PULSE_DOPPLER RADAR" are one name."""
    text = unicodedata.normalize("NFKC", name).casefold()
    for symbol, word in _SYMBOLS.items():
        text = text.replace(symbol, word)
    slug = _NON_WORD.sub("-", text).strip("-")
    if not slug:
        raise ValidationError(f"{name!r} is not a usable name")
    return slug[:MAX_SLUG].rstrip("-")


def alias_tag(name: str) -> str:
    return ALIAS_PREFIX + entity_slug(name)


def type_tag(entity_type: str) -> str:
    return TYPE_PREFIX + entity_slug(entity_type)


def aliases_of(tags: frozenset[str]) -> frozenset[str]:
    """The slugs under which an entity is known."""
    return frozenset(t[len(ALIAS_PREFIX) :] for t in tags if t.startswith(ALIAS_PREFIX))
