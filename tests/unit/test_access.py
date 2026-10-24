import pytest
from hypothesis import given
from hypothesis import strategies as st

from hive_mcp.domain.access import Acl, Tier, can_create, can_write, read_filter
from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility, check_visibility

IDS = st.sampled_from(["a", "b", "c"])
ORGS = st.sampled_from(["o1", "o2"])
principals = st.builds(
    Principal,
    id=IDS,
    org_id=ORGS,
    role=st.sampled_from(list(Role)),
    teams=st.frozensets(IDS),
    projects=st.frozensets(IDS),
)
scopes = st.builds(Scope, level=st.sampled_from(list(ScopeLevel)), owner=IDS | ORGS)


@st.composite
def acls(draw):
    scope = draw(scopes)
    vis = (
        Visibility.SCOPE
        if scope.level is ScopeLevel.PRIVATE
        else draw(st.sampled_from(list(Visibility)))
    )
    return Acl(draw(ORGS), scope, vis)


@given(principals, acls())
def test_never_visible_across_tenants(p, acl):
    if acl.org_id != p.org_id:
        assert read_filter(p).tier(acl) is None
        assert not can_create(p, acl)


@given(principals, acls())
def test_primary_iff_member_and_referenced_only_when_org_visible(p, acl):
    tier = read_filter(p).tier(acl)
    member = acl.org_id == p.org_id and p.is_member(acl.scope)
    assert (tier is Tier.PRIMARY) == member
    if tier is Tier.REFERENCED:
        assert acl.visibility is Visibility.ORG and not member


@given(principals, acls(), IDS)
def test_write_implies_create_implies_membership_and_role(p, acl, author):
    if can_write(p, acl, author):
        assert can_create(p, acl) and p.role >= Role.MEMBER
    if can_create(p, acl):
        assert read_filter(p).tier(acl) is Tier.PRIMARY


@given(principals, acls(), IDS)
def test_private_items_are_writable_only_by_their_owner(p, acl, author):
    if acl.scope.level is ScopeLevel.PRIVATE and can_write(p, acl, author):
        assert acl.scope.owner == p.id


def test_viewers_never_write():
    viewer = Principal("a", "o1", Role.VIEWER, projects=frozenset({"p"}))
    acl = Acl("o1", Scope(ScopeLevel.PROJECT, "p"), Visibility.SCOPE)
    assert read_filter(viewer).tier(acl) is Tier.PRIMARY
    assert not can_create(viewer, acl) and not can_write(viewer, acl, "a")


def test_invalid_private_org_visibility_rejected_everywhere():
    private = Scope(ScopeLevel.PRIVATE, "a")
    with pytest.raises(ValidationError):
        check_visibility(private, Visibility.ORG)
    with pytest.raises(ValidationError):
        Acl("o1", private, Visibility.ORG)
