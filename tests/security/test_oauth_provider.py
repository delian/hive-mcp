import asyncio

import pytest
from mcp.server.auth.provider import (
    AuthorizationParams,
    AuthorizeError,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from hive_mcp.domain.principal import Role
from hive_mcp.security.directory import DirectoryUser
from tests.security.conftest import REDIRECT, RESOURCE, make_client, query


def params(**kw):
    base = dict(
        state="st",
        scopes=None,
        code_challenge="c" * 43,
        redirect_uri=AnyUrl(REDIRECT),
        redirect_uri_provided_explicitly=True,
        resource=None,
    )
    return AuthorizationParams(**{**base, **kw})


# ---------------------------------------------------------------- clients
@pytest.mark.parametrize(
    "uri",
    [
        "https://app.example/cb",
        "http://localhost:8123/cb",
        "http://127.0.0.1/cb",
        "cursor://anysphere.cursor/oauth",
        "vscode://ext/callback",
    ],
)
async def test_acceptable_redirect_uris(rig, uri):
    client = OAuthClientInformationFull(
        client_id="c-" + uri[:6], redirect_uris=[AnyUrl(uri)], token_endpoint_auth_method="none"
    )
    await rig.provider.register_client(client)
    assert await rig.provider.get_client(client.client_id) is not None


@pytest.mark.parametrize(
    "uri",
    [
        "http://evil.example/cb",
        "javascript:alert(1)",
        "data:text/html,x",
        "file:///etc/passwd",
        "https://app.example/cb#frag",
    ],
)
async def test_dangerous_redirect_uris_are_refused(rig, uri):
    client = OAuthClientInformationFull(
        client_id="bad", redirect_uris=[AnyUrl(uri)], token_endpoint_auth_method="none"
    )
    with pytest.raises(RegistrationError):
        await rig.provider.register_client(client)
    assert await rig.provider.get_client("bad") is None


async def test_duplicate_registration_and_idle_client_expiry(rig):
    with pytest.raises(RegistrationError):
        await rig.provider.register_client(make_client())  # same client_id again
    rig.clock.advance(days=31)
    assert (
        await rig.provider.get_client(rig.client.client_id) is None
    )  # never-used registrations expire


# ---------------------------------------------------------------- authorize / consent hand-off
async def test_authorize_binds_the_resource_and_scope(rig):
    assert "/oauth/start?txn=" in await rig.provider.authorize(
        rig.client, params(resource=RESOURCE)
    )
    with pytest.raises(AuthorizeError) as err:
        await rig.provider.authorize(rig.client, params(resource="https://other.example/mcp"))
    assert err.value.error == "invalid_target"
    with pytest.raises(AuthorizeError) as err:
        await rig.provider.authorize(rig.client, params(scopes=["hive", "admin"]))
    assert err.value.error == "invalid_scope"


async def test_login_transaction_expires_and_is_single_use(rig):
    url = await rig.provider.authorize(rig.client, params())
    txn_id = query(url)["txn"]
    assert (await rig.provider.load_txn(txn_id))["user"] is None
    rig.clock.advance(seconds=601)
    assert await rig.provider.load_txn(txn_id) is None
    code = await rig.code_for()
    assert code
    txn_id = query(await rig.provider.authorize(rig.client, params()))["txn"]
    txn = await rig.provider.load_txn(txn_id)
    await rig.provider.attach_user(txn_id, txn, "alice@acme.com")
    assert await rig.provider.finish(txn_id, approve=True) is not None
    assert await rig.provider.finish(txn_id, approve=True) is None  # consent cannot be replayed


async def test_denial_and_unauthenticated_finish_never_issue_a_code(rig):
    txn_id = query(await rig.provider.authorize(rig.client, params(state="xyz")))["txn"]
    txn = await rig.provider.load_txn(txn_id)
    await rig.provider.attach_user(txn_id, txn, "alice@acme.com")
    denied = await rig.provider.finish(txn_id, approve=False)
    assert query(denied) == {"error": "access_denied", "state": "xyz"}
    txn_id = query(await rig.provider.authorize(rig.client, params()))[
        "txn"
    ]  # nobody logged in yet
    assert "error=access_denied" in await rig.provider.finish(txn_id, approve=True)


# ---------------------------------------------------------------- authorization codes
async def test_code_is_bound_to_its_client_and_expires(rig):
    code = await rig.code_for()
    other = await rig.new_client("other")
    assert await rig.provider.load_authorization_code(other, code) is None
    assert await rig.provider.load_authorization_code(rig.client, "no-such-code") is None
    loaded = await rig.provider.load_authorization_code(rig.client, code)
    assert (
        loaded.subject == "alice@acme.com"
        and loaded.resource == RESOURCE
        and loaded.scopes == ["hive"]
    )
    rig.clock.advance(seconds=301)
    assert await rig.provider.load_authorization_code(rig.client, code) is None


async def test_replaying_a_spent_code_revokes_everything_it_produced(rig):
    code = await rig.code_for()
    loaded = await rig.provider.load_authorization_code(rig.client, code)
    tokens = await rig.provider.exchange_authorization_code(rig.client, loaded)
    assert await rig.provider.load_access_token(tokens.access_token) is not None
    assert await rig.provider.load_authorization_code(rig.client, code) is None  # spent
    assert (
        await rig.provider.load_access_token(tokens.access_token) is None
    )  # and the thief gets nothing
    assert await rig.provider.load_refresh_token(rig.client, tokens.refresh_token) is None


async def test_concurrent_exchange_of_one_code_issues_tokens_once(rig):
    code = await rig.code_for()
    loaded = await rig.provider.load_authorization_code(rig.client, code)
    results = await asyncio.gather(
        *(rig.provider.exchange_authorization_code(rig.client, loaded) for _ in range(8)),
        return_exceptions=True,
    )
    winners = [r for r in results if not isinstance(r, Exception)]
    assert len(winners) == 1 and all(isinstance(r, TokenError) for r in results if r not in winners)
    assert (
        await rig.provider.load_access_token(winners[0].access_token) is None
    )  # the race itself revokes


async def test_exchange_refuses_a_user_removed_between_consent_and_exchange(rig):
    code = await rig.code_for()
    loaded = await rig.provider.load_authorization_code(rig.client, code)
    rig.directory.remove("alice@acme.com")
    with pytest.raises(TokenError):
        await rig.provider.exchange_authorization_code(rig.client, loaded)


# ---------------------------------------------------------------- access tokens
async def test_access_token_carries_live_directory_claims(rig):
    tokens = await rig.tokens()
    access = await rig.provider.load_access_token(tokens.access_token)
    assert (
        access.subject == "alice@acme.com"
        and access.resource == RESOURCE
        and access.scopes == ["hive"]
    )
    assert access.claims["hive_org"] == "acme" and access.claims["hive_role"] == "member"
    assert access.claims["hive_projects"] == ["p1"] and access.claims["hive_teams"] == ["t1"]
    assert tokens.expires_in == 3600 and tokens.token_type == "Bearer" and tokens.scope == "hive"
    rig.directory.upsert(
        DirectoryUser("alice@acme.com", "acme", Role.VIEWER, projects=frozenset({"p9"}))
    )
    changed = await rig.provider.load_access_token(tokens.access_token)  # no new login needed
    assert changed.claims["hive_role"] == "viewer" and changed.claims["hive_projects"] == ["p9"]


async def test_removed_or_disabled_users_lose_access_immediately(rig):
    tokens = await rig.tokens()
    rig.directory.upsert(DirectoryUser("alice@acme.com", "acme", Role.MEMBER, disabled=True))
    assert await rig.provider.load_access_token(tokens.access_token) is None
    rig.directory.upsert(DirectoryUser("alice@acme.com", "acme", Role.MEMBER))
    assert await rig.provider.load_access_token(tokens.access_token) is not None
    rig.directory.remove("alice@acme.com")
    assert await rig.provider.load_access_token(tokens.access_token) is None


async def test_access_token_expiry_and_garbage(rig):
    tokens = await rig.tokens()
    rig.clock.advance(seconds=3599)
    assert await rig.provider.load_access_token(tokens.access_token) is not None
    rig.clock.advance(seconds=2)
    assert await rig.provider.load_access_token(tokens.access_token) is None
    for junk in (
        "",
        "hat_",
        "hat_" + "x" * 43,
        "hrt_" + "x" * 43,
        tokens.refresh_token,
        "Bearer x",
    ):
        assert (
            await rig.provider.load_access_token(junk) is None
        )  # a refresh token is not an access token


async def test_tokens_are_stored_hashed(rig):
    tokens = await rig.tokens()
    dump = repr(rig.provider.kv._rows)
    assert tokens.access_token not in dump and tokens.refresh_token not in dump


# ---------------------------------------------------------------- refresh tokens
async def test_refresh_rotates_both_tokens(rig):
    first = await rig.tokens()
    refresh = await rig.provider.load_refresh_token(rig.client, first.refresh_token)
    second = await rig.provider.exchange_refresh_token(rig.client, refresh, [])
    assert second.access_token != first.access_token and second.refresh_token != first.refresh_token
    assert await rig.provider.load_access_token(second.access_token) is not None
    assert await rig.provider.load_refresh_token(rig.client, second.refresh_token) is not None


async def test_reusing_a_rotated_refresh_token_revokes_the_whole_family(rig):
    first = await rig.tokens()
    old = await rig.provider.load_refresh_token(rig.client, first.refresh_token)
    second = await rig.provider.exchange_refresh_token(rig.client, old, [])
    assert (
        await rig.provider.load_refresh_token(rig.client, first.refresh_token) is None
    )  # thief replays it
    assert (
        await rig.provider.load_access_token(second.access_token) is None
    )  # legitimate session is cut too
    assert await rig.provider.load_refresh_token(rig.client, second.refresh_token) is None


async def test_concurrent_refresh_with_one_token_has_one_winner(rig):
    first = await rig.tokens()
    old = await rig.provider.load_refresh_token(rig.client, first.refresh_token)
    results = await asyncio.gather(
        *(rig.provider.exchange_refresh_token(rig.client, old, []) for _ in range(8)),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1


async def test_refresh_scope_client_expiry_and_user_checks(rig):
    tokens = await rig.tokens()
    refresh = await rig.provider.load_refresh_token(rig.client, tokens.refresh_token)
    other = await rig.new_client("other")
    assert (
        await rig.provider.load_refresh_token(other, tokens.refresh_token) is None
    )  # another client's token
    with pytest.raises(TokenError) as err:
        await rig.provider.exchange_refresh_token(rig.client, refresh, ["hive", "admin"])
    assert err.value.error == "invalid_scope"
    tokens = await rig.tokens()
    refresh = await rig.provider.load_refresh_token(rig.client, tokens.refresh_token)
    rig.directory.remove("alice@acme.com")
    with pytest.raises(TokenError):
        await rig.provider.exchange_refresh_token(rig.client, refresh, [])
    rig.directory.upsert(DirectoryUser("alice@acme.com", "acme", Role.MEMBER))
    tokens = await rig.tokens()
    rig.clock.advance(days=31)
    assert await rig.provider.load_refresh_token(rig.client, tokens.refresh_token) is None


async def test_revoking_either_token_kills_the_family(rig):
    tokens = await rig.tokens()
    access = await rig.provider.load_access_token(tokens.access_token)
    await rig.provider.revoke_token(access)
    assert await rig.provider.load_access_token(tokens.access_token) is None
    assert await rig.provider.load_refresh_token(rig.client, tokens.refresh_token) is None
    again = await rig.tokens()
    refresh = await rig.provider.load_refresh_token(rig.client, again.refresh_token)
    await rig.provider.revoke_token(refresh)
    assert await rig.provider.load_access_token(again.access_token) is None
    await rig.provider.revoke_token(access)  # revoking twice is harmless


async def test_users_do_not_share_families(rig):
    a, b = await rig.tokens("alice@acme.com"), await rig.tokens("bob@acme.com")
    await rig.provider.revoke_token(await rig.provider.load_access_token(a.access_token))
    assert await rig.provider.load_access_token(b.access_token) is not None


async def test_the_enterprise_assertion_grant_is_refused(rig):
    with pytest.raises(TokenError) as err:
        await rig.provider.exchange_identity_assertion(rig.client, None)
    assert err.value.error == "unsupported_grant_type"
