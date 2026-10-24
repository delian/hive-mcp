"""SQL translation of ScopeFilter (psycopg placeholders). A prefilter: the tier is decided by ScopeFilter.tier."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hive_mcp.domain.access import ScopeFilter


def member_where(flt: ScopeFilter, alias: str = "") -> tuple[str, list[object]]:
    p = f"{alias}." if alias else ""
    scopes = sorted((int(s.level), s.owner) for s in flt.member_scopes)
    if not scopes:
        return "false", []
    levels, owners = [lvl for lvl, _ in scopes], [owner for _, owner in scopes]
    # `p` is a constant alias prefix; all values travel as bound parameters
    sql = f"({p}scope_level, {p}scope_owner) IN (SELECT * FROM unnest(%s::smallint[], %s::text[]))"  # noqa: S608
    return sql, [levels, owners]


def acl_where(flt: ScopeFilter, alias: str = "") -> tuple[str, list[object]]:
    p = f"{alias}." if alias else ""
    member, params = member_where(flt, alias)
    return f"{p}org_id = %s AND ({member} OR {p}visibility = 'org')", [flt.org_id, *params]
