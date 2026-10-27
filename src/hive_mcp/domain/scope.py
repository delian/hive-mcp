"""Where knowledge lives (scope) and who may read it beyond that scope (visibility)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.text import check_text


class ScopeLevel(IntEnum):
    """Narrow to wide. The integer is the rank used for relevance weighting."""

    PRIVATE = 0
    PROJECT = 1
    TEAM = 2
    ORG = 3


class Visibility(StrEnum):
    SCOPE = "scope"  # only members of the scope
    ORG = "org"  # members see it as primary; everyone else in the org as *referenced*


@dataclass(frozen=True, slots=True)
class Scope:
    """`owner` is the user id (PRIVATE), project id, team id, or org id (ORG)."""

    level: ScopeLevel
    owner: str

    def __post_init__(self) -> None:
        if not self.owner:
            raise ValidationError("scope owner must not be empty")
        check_text(self.owner, what="scope owner")


def check_visibility(scope: Scope, visibility: Visibility) -> None:
    """Private knowledge can never be org-visible: it must be promoted (moved) first."""
    if scope.level is ScopeLevel.PRIVATE and visibility is not Visibility.SCOPE:
        raise ValidationError("private items cannot be org-visible; promote them first")


def scope_key(scope: Scope) -> str:
    """Text form used in tokens, the user directory and the CLI, e.g. `team:t1`."""
    return f"{scope.level.name.lower()}:{scope.owner}"


def parse_scope(text: str) -> Scope:
    level, _, owner = text.partition(":")
    try:
        return Scope(ScopeLevel[level.strip().upper()], owner.strip())
    except KeyError:
        pass
    raise ValidationError(
        f"scope must look like team:<id>, project:<id> or org:<id> (got {text!r})"
    )
