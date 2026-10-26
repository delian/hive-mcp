import pytest

from hive_mcp.domain.errors import PermissionDenied, ValidationError
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel, parse_scope, scope_key
from hive_mcp.security.directory import DirectoryUser
from hive_mcp.security.principals import claims_for, principal_from_claims

TEAM = Scope(ScopeLevel.TEAM, "t1")


def test_scope_text_roundtrip_and_errors():
    for scope in (TEAM, Scope(ScopeLevel.ORG, "acme"), Scope(ScopeLevel.PROJECT, "a:b")):
        assert parse_scope(scope_key(scope)) == scope
    for bad in ("", "team", "team:", "galaxy:x", ":x"):
        with pytest.raises(ValidationError):
            parse_scope(bad)


def test_who_can_approve_what():
    admin = Principal("a", "o", Role.ADMIN)
    named = Principal("n", "o", Role.MEMBER, approves=frozenset({TEAM}))
    viewer = Principal("v", "o", Role.VIEWER, approves=frozenset({TEAM}))
    plain = Principal("p", "o", Role.EDITOR)
    other = Scope(ScopeLevel.TEAM, "t2")
    assert admin.can_approve(TEAM) and admin.can_approve(other) and admin.is_approver
    assert named.can_approve(TEAM) and not named.can_approve(other) and named.is_approver
    assert not viewer.can_approve(TEAM) and not viewer.is_approver  # read-only roles never approve
    assert not plain.can_approve(TEAM) and not plain.is_approver
    assert not admin.can_approve(
        Scope(ScopeLevel.PRIVATE, "a")
    )  # nothing to approve in a private space


def test_approver_scopes_survive_tokens_and_the_directory():
    p = Principal("n", "o", Role.MEMBER, approves=frozenset({TEAM}))
    assert principal_from_claims("n", claims_for(p)).approves == {TEAM}
    user = DirectoryUser("n@x.io", "o", approves=frozenset({"team:t1"}))
    assert DirectoryUser.from_json(user.to_json()).principal().approves == {TEAM}
    assert (
        DirectoryUser.from_json({"email": "n@x.io", "org_id": "o"}).approves == frozenset()
    )  # old files
    claims = {**claims_for(p), "hive_approves": ["nonsense"]}
    with pytest.raises(PermissionDenied):
        principal_from_claims("n", claims)
