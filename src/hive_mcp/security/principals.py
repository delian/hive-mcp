"""Token claims -> Principal: the only way identity enters; tools never take a user argument."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hive_mcp.domain.errors import PermissionDenied, ValidationError
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, parse_scope, scope_key

if TYPE_CHECKING:
    from collections.abc import Mapping

MAX_MEMBERSHIPS = 1000
MAX_ID_LEN = 200


def _id(value: object, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_ID_LEN or "\x00" in value:
        raise PermissionDenied(f"token has an invalid {what}")
    return value


def _ids(value: object, what: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list | tuple | set | frozenset) or len(value) > MAX_MEMBERSHIPS:
        raise PermissionDenied(f"token has an invalid {what}")
    return frozenset(_id(v, what) for v in value)


def _scopes(value: object) -> frozenset[Scope]:
    try:
        return frozenset(parse_scope(s) for s in _ids(value, "approver scopes"))
    except ValidationError:
        raise PermissionDenied("token has an invalid approver scope") from None


def principal_from_claims(subject: str | None, claims: Mapping[str, Any] | None) -> Principal:
    """Strict: a missing org or subject is refused, and an absent/unknown role means read-only."""
    claims = claims or {}
    role_name = claims.get("hive_role", "viewer")
    try:
        role = Role[str(role_name).upper()]
    except KeyError:
        role = Role.VIEWER  # unknown roles never gain write access
    return Principal(
        id=_id(subject, "subject"),
        org_id=_id(claims.get("hive_org"), "organisation"),
        role=role,
        teams=_ids(claims.get("hive_teams"), "teams"),
        projects=_ids(claims.get("hive_projects"), "projects"),
        approves=_scopes(claims.get("hive_approves")),
    )


def claims_for(principal: Principal, label: str = "") -> dict[str, Any]:
    """Inverse of `principal_from_claims` (used by the static token store)."""
    return {
        "hive_org": principal.org_id,
        "hive_role": principal.role.name.lower(),
        "hive_teams": sorted(principal.teams),
        "hive_projects": sorted(principal.projects),
        "hive_approves": sorted(scope_key(s) for s in principal.approves),
        "label": label,
    }
