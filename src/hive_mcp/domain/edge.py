"""Typed links between knowledge items (the graph); visible only to those who see both ends."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 - runtime-evaluated dataclass field

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.text import has_bad_text

RELATION = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
# Suggested vocabulary; any slug is accepted so teams can add their own.
COMMON_RELATIONS = (
    "related",
    "part_of",
    "next",
    "derived_from",
    "supports",
    "contradicts",
    "supersedes",
    "mentions",
)


def normalize_relation(value: str) -> str:
    relation = value.strip().lower().replace(" ", "_")
    if not RELATION.match(relation):
        raise ValidationError("relation must be a short lowercase word like 'derived_from'")
    return relation


@dataclass(frozen=True, slots=True)
class Edge:
    org_id: str
    src: str
    dst: str
    relation: str
    author_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.src or not self.dst or self.src == self.dst:
            raise ValidationError("an edge needs two different items")
        if has_bad_text(self.src, self.dst) or not RELATION.match(self.relation):
            raise ValidationError("invalid edge")

    @property
    def key(self) -> tuple[str, str, str]:
        return self.src, self.dst, self.relation
