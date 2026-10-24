"""SQL translation of ScopeFilter. Only a *prefilter*: the tier is always decided by ScopeFilter.tier."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hive_mcp.domain.access import ScopeFilter


def member_where(flt: ScopeFilter, alias: str = "") -> tuple[str, list[object]]:
    """Rows whose scope is one of the caller's own (PRIMARY tier)."""
    p = f"{alias}." if alias else ""
    scopes = sorted((int(s.level), s.owner) for s in flt.member_scopes)
    if not scopes:
        return "0", []
    marks = ",".join("(?,?)" for _ in scopes)
    params: list[object] = [v for pair in scopes for v in pair]
    return f"({p}scope_level, {p}scope_owner) IN (VALUES {marks})", params


def acl_where(flt: ScopeFilter, alias: str = "") -> tuple[str, list[object]]:
    """Tenant boundary AND (own scope OR org-visible)."""
    p = f"{alias}." if alias else ""
    member, params = member_where(flt, alias)
    return f"{p}org_id = ? AND ({member} OR {p}visibility = 'org')", [flt.org_id, *params]
