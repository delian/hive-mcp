"""The OAuth token service, exactly: records, lifetimes, rotation, reuse detection, live users."""

from __future__ import annotations

import pytest
from mcp.server.auth.provider import TokenError
from mcp.shared.auth import OAuthClientInformationFull

from hive_mcp.adapters.memory.kv import MemoryKv
from hive_mcp.domain.clock import FixedClock
from hive_mcp.domain.principal import Role
from hive_mcp.security.directory import DirectoryUser
from hive_mcp.security.oauth.models import (
    ACCESS_PREFIX,
    NS_ACCESS,
    NS_FAMILY,
    NS_REFRESH,
    NS_REFRESH_USED,
    REFRESH_PREFIX,
    OAuthConfig,
    digest,
)
from hive_mcp.security.oauth.tokens import TokenService
from hive_mcp.security.principals import claims_for

ANN = DirectoryUser("ann@acme.com", "acme", Role.MEMBER, projects=frozenset({"p1"}))


class Directory:
    def __init__(self, users: list[DirectoryUser]) -> None:
        self.users = {u.email: u for u in users}

    def lookup(self, email: str):
        return self.users.get(email)


def client(client_id: str = "client-1") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(client_id=client_id, redirect_uris=["https://c.example/cb"])


@pytest.fixture
def world():
    clock = FixedClock()
    kv = MemoryKv(clock)
    directory = Directory([ANN])
    config = OAuthConfig("https://hive.example", "https://hive.example/mcp", 100, 1000)
    return TokenService(lambda: kv, directory, config, clock), kv, clock, directory


async def issue(service, family="fam", scopes=("hive",), resource="https://hive.example/mcp"):
    await service.start_family(family, "ann@acme.com")
    return await service.issue("ann@acme.com", "client-1", list(scopes), resource, family)


async def test_a_family_is_live_until_revoked_and_unknown_ones_count_as_revoked(world):
    service, kv, clock, _ = world
    assert await service.revoked("nope") is True
    await service.start_family("fam", "ann@acme.com")
    record = await kv.get(NS_FAMILY, "fam")
    assert record == {"revoked": False, "user": "ann@acme.com"}
    assert await service.revoked("fam") is False
    await service.revoke_family("fam")
    assert await kv.get(NS_FAMILY, "fam") == {"revoked": True, "user": "ann@acme.com"}
    assert await service.revoked("fam") is True
    await service.revoke_family("never-started")  # also remembered as revoked
    assert (await kv.get(NS_FAMILY, "never-started")) == {"revoked": True}
    clock.advance(seconds=1001)  # the family record ages out with the refresh lifetime
    assert await kv.get(NS_FAMILY, "fam") is None


async def test_issuing_makes_prefixed_hashed_records_with_the_right_lifetimes(world):
    service, kv, clock, _ = world
    token = await issue(service, scopes=("hive", "extra"))
    assert token.access_token.startswith(ACCESS_PREFIX) and token.refresh_token.startswith(
        REFRESH_PREFIX
    )
    assert len(token.access_token) == len(ACCESS_PREFIX) + 43
    assert len(token.refresh_token) == len(REFRESH_PREFIX) + 43
    assert token.token_type == "Bearer" and token.expires_in == 100 and token.scope == "hive extra"
    now = int(clock.now().timestamp())
    access = await kv.get(NS_ACCESS, digest(token.access_token))
    assert access == {
        "client_id": "client-1", "user": "ann@acme.com", "scopes": ["hive", "extra"],
        "resource": "https://hive.example/mcp", "family": "fam", "expires_at": now + 100,
    }  # fmt: skip
    refresh = await kv.get(NS_REFRESH, digest(token.refresh_token))
    assert refresh["expires_at"] == now + 1000 and refresh["family"] == "fam"
    assert await kv.get(NS_ACCESS, token.access_token) is None  # never stored in the clear
    clock.advance(seconds=101)
    assert await kv.get(NS_ACCESS, digest(token.access_token)) is None  # the access record expires
    assert await kv.get(NS_REFRESH, digest(token.refresh_token)) is not None
    clock.advance(seconds=900)
    assert await kv.get(NS_REFRESH, digest(token.refresh_token)) is None


async def test_loading_an_access_token_checks_family_user_and_builds_live_claims(world):
    service, _kv, _clock, directory = world
    token = await issue(service)
    got = await service.load_access(token.access_token)
    assert got is not None and got.token == token.access_token and got.client_id == "client-1"
    assert got.scopes == ["hive"] and got.resource == "https://hive.example/mcp"
    assert got.subject == "ann@acme.com" and got.family == "fam"
    assert got.expires_at == int(service.now()) + 100
    assert got.claims == claims_for(ANN.principal()) | {"iss": "https://hive.example"}
    assert await service.load_access("hat_unknown") is None
    directory.users.clear()  # the person was removed: the token stops working at once
    assert await service.load_access(token.access_token) is None
    directory.users["ann@acme.com"] = ANN
    await service.revoke_family("fam")
    assert await service.load_access(token.access_token) is None


async def test_loading_a_refresh_token_needs_the_same_client_and_a_live_family(world):
    service, _kv, _clock, _ = world
    token = await issue(service)
    got = await service.load_refresh(client(), token.refresh_token)
    assert got is not None and got.token == token.refresh_token and got.client_id == "client-1"
    assert got.scopes == ["hive"] and got.subject == got.user == "ann@acme.com"
    assert got.family == "fam" and got.resource == "https://hive.example/mcp"
    assert got.expires_at == service.now() + 1000
    assert await service.load_refresh(client("other"), token.refresh_token) is None
    assert await service.load_refresh(client(), "hrt_unknown") is None
    await service.revoke_family("fam")
    assert await service.load_refresh(client(), token.refresh_token) is None


async def test_rotation_spends_the_old_token_issues_new_ones_and_keeps_the_family(world):
    service, kv, _clock, _ = world
    first = await issue(service, scopes=("hive", "extra"))
    refresh = await service.load_refresh(client(), first.refresh_token)
    second = await service.rotate(client(), refresh, [])
    assert second.refresh_token != first.refresh_token and second.scope == "hive extra"  # kept
    assert await kv.get(NS_REFRESH, digest(first.refresh_token)) is None
    assert await kv.get(NS_REFRESH_USED, digest(first.refresh_token)) == {"family": "fam"}
    new_access = await kv.get(NS_ACCESS, digest(second.access_token))
    assert new_access["client_id"] == "client-1" and new_access["family"] == "fam"
    assert new_access["resource"] == "https://hive.example/mcp"
    narrowed = await service.rotate(
        client(), await service.load_refresh(client(), second.refresh_token), ["hive"]
    )
    assert narrowed.scope == "hive"  # a narrower scope is allowed


async def test_widening_the_scope_is_refused_and_leaves_the_token_usable(world):
    service, _kv, _clock, _ = world
    token = await issue(service)
    refresh = await service.load_refresh(client(), token.refresh_token)
    with pytest.raises(TokenError) as caught:
        await service.rotate(client(), refresh, ["hive", "admin"])
    assert caught.value.error == "invalid_scope"
    assert caught.value.error_description == "cannot widen the scope of a refresh token"
    assert await service.load_refresh(client(), token.refresh_token) is not None


async def test_using_a_refresh_token_twice_revokes_the_whole_family(world, caplog):
    service, _kv, _clock, _ = world
    token = await issue(service)
    stale = await service.load_refresh(client(), token.refresh_token)
    await service.rotate(client(), stale, [])
    with pytest.raises(TokenError) as concurrent:  # the same token used again at the same moment
        await service.rotate(client(), stale, [])
    assert concurrent.value.error == "invalid_grant"
    assert concurrent.value.error_description == "refresh token already used"
    assert await service.revoked("fam") is True
    # and a spent token coming back later is also theft: a new family dies on it
    other = await issue(service, family="fam2")
    spent = await service.load_refresh(client(), other.refresh_token)
    await service.rotate(client(), spent, [])
    with caplog.at_level("WARNING", logger="hive.security"):
        assert await service.load_refresh(client(), other.refresh_token) is None
    assert await service.revoked("fam2") is True
    assert [r.getMessage() for r in caplog.records] == [
        "oauth.refresh_reuse client=client-1 family=fam2"
    ]


async def test_a_removed_person_cannot_rotate_and_the_message_says_so(world):
    service, _kv, _clock, directory = world
    token = await issue(service)
    refresh = await service.load_refresh(client(), token.refresh_token)
    directory.users.clear()
    with pytest.raises(TokenError) as caught:
        await service.rotate(client(), refresh, [])
    assert caught.value.error == "invalid_grant"
    assert caught.value.error_description == "user is no longer permitted"


async def test_family_and_spent_token_records_age_out_with_the_refresh_lifetime(world):
    service, kv, clock, _ = world
    token = await issue(service)
    refresh = await service.load_refresh(client(), token.refresh_token)
    await service.rotate(client(), refresh, [])
    assert await kv.get(NS_REFRESH_USED, digest(token.refresh_token)) is not None
    assert await kv.get(NS_FAMILY, "fam") is not None
    clock.advance(seconds=999)
    assert await kv.get(NS_FAMILY, "fam") is not None
    clock.advance(seconds=2)
    assert await kv.get(NS_FAMILY, "fam") is None
    assert await kv.get(NS_REFRESH_USED, digest(token.refresh_token)) is None


async def test_a_client_without_an_id_issues_tokens_with_an_empty_client_id(world):
    service, kv, _clock, _ = world
    token = await issue(service)
    refresh = await service.load_refresh(client(), token.refresh_token)
    anonymous = OAuthClientInformationFull.model_construct(client_id=None, redirect_uris=[])
    renewed = await service.rotate(anonymous, refresh, [])
    record = await kv.get(NS_ACCESS, digest(renewed.access_token))
    assert record["client_id"] == ""
