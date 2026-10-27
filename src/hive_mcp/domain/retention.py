"""Retention rules: "delete private notes untouched for two years", "archive notes after a year".

`purge` (the default) deletes for good; `archive` only hides: the item leaves search and lists but
stays in the database and in its history, and a person can restore it. Age counts from the last
change (an archive is itself a change, so an archived item is not archived again). A rule must name
a kind or a scope level, so no single typo can match everything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.item import Kind
from hive_mcp.domain.scope import ScopeLevel

MIN_DAYS = 1
MAX_DAYS = 36500
_KNOWN = {"kind", "scope", "days", "action"}
ACTIONS = ("purge", "archive")


@dataclass(frozen=True, slots=True)
class RetentionRule:
    days: int
    kind: Kind | None = None
    scope_level: ScopeLevel | None = None
    action: str = "purge"

    def __post_init__(self) -> None:
        if not MIN_DAYS <= self.days <= MAX_DAYS:
            raise ValidationError(f"retention days must be {MIN_DAYS}-{MAX_DAYS}")
        if self.action not in ACTIONS:
            raise ValidationError(f"retention action must be one of {ACTIONS}")
        if self.kind is None and self.scope_level is None:
            raise ValidationError("a retention rule needs a kind or a scope (or both)")

    def describe(self) -> str:
        what = str(self.kind) if self.kind else "any kind"
        where = self.scope_level.name.lower() if self.scope_level is not None else "any scope"
        return f"{self.action} {what} in {where}, older than {self.days} days"


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
            action = str(entry.get("action", "purge"))
        except (KeyError, ValueError, TypeError) as exc:
            raise ValidationError(f"retention rule {entry!r}: unknown kind, scope or days") from exc
        rules.append(RetentionRule(days, kind, level, action))
    return rules
