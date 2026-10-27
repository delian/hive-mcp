"""Retention rules: "delete private notes untouched for two years", "keep sessions 90 days".

Rules delete for good (purge): the reversible path for stale knowledge is the owner's review
(`kb_review_stale`). Age counts from the last change, or from creation for session logs, which are
never edited. A rule must name a kind or a scope level, so no single typo can match everything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.item import Kind
from hive_mcp.domain.scope import ScopeLevel

MIN_DAYS = 1
MAX_DAYS = 36500
_KNOWN = {"kind", "scope", "days"}


@dataclass(frozen=True, slots=True)
class RetentionRule:
    days: int
    kind: Kind | None = None
    scope_level: ScopeLevel | None = None

    def __post_init__(self) -> None:
        if not MIN_DAYS <= self.days <= MAX_DAYS:
            raise ValidationError(f"retention days must be {MIN_DAYS}-{MAX_DAYS}")
        if self.kind is None and self.scope_level is None:
            raise ValidationError("a retention rule needs a kind or a scope (or both)")

    def describe(self) -> str:
        what = str(self.kind) if self.kind else "any kind"
        where = self.scope_level.name.lower() if self.scope_level is not None else "any scope"
        return f"{what} in {where}, older than {self.days} days"


def parse_rules(raw: list[dict[str, Any]]) -> list[RetentionRule]:
    """From settings: [{"kind": "session", "days": 90}, {"scope": "private", "days": 730}]."""
    rules = []
    for entry in raw:
        if not isinstance(entry, dict) or "days" not in entry or set(entry) - _KNOWN:
            raise ValidationError(f"retention rule {entry!r}: use only kind, scope and days")
        try:
            kind = Kind(entry["kind"]) if entry.get("kind") else None
            level = ScopeLevel[str(entry["scope"]).upper()] if entry.get("scope") else None
            days = int(entry["days"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValidationError(f"retention rule {entry!r}: unknown kind, scope or days") from exc
        rules.append(RetentionRule(days, kind, level))
    return rules
