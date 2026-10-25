"""OAuth end to end: real server, fake Google, scripted browser and client."""

import re
import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from hive_mcp.domain.principal import Principal, Role
from hive_mcp.security.directory import DirectoryUser
from tests.integration import oauth_driver as d
from tests.integration.conftest import Live, _oauth_server

ACCEPT = {"Accept": "application/json, text/event-stream"}


def mcp_status(live: Live, token: str) -> int:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    return httpx.post(
        f"{live.url}/mcp", json=body, headers={**ACCEPT, "Authorization": f"Bearer {token}"}
    ).status_code


def login(live, **kw):
    client_id = d.register(live).json()["client_id"]
    verifier, challenge = d.pkce()
    return client_id, verifier, d.full_login(live, client_id, challenge, **kw)


# ---------------------------------------------------------------- discovery
def test_discovery_documents_describe_a_pkce_only_server(oauth_live):
    meta = httpx.get(f"{oauth_live.url}/.well-known/oauth-authorization-server").json()
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["registration_endpoint"].endswith("/register") and meta[
        "revocation_endpoint"
    ].endswith("/revoke")
    assert (
        "authorization_code" in meta["grant_types_supported"]
        and "implicit" not in meta["grant_types_supported"]
    )
    prm = httpx.get(f"{oauth_live.url}/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == f"{oauth_live.url}/mcp" and prm["authorization_servers"] == [
        oauth_live.url
    ]
    challenge = httpx.post(f"{oauth_live.url}/mcp", json={}, headers=ACCEPT).headers[
        "www-authenticate"
    ]
    assert "resource_metadata=" in challenge


def test_oauth_endpoints_do_not_exist_when_sign_in_is_not_configured(live):
    assert httpx.post(f"{live.url}/register", json={}).status_code == 404
    assert httpx.get(f"{live.url}/authorize").status_code == 404
    assert httpx.get(f"{live.url}/oauth/start?txn=x").status_code == 404


# ---------------------------------------------------------------- the happy path
async def test_login_yields_a_token_that_works_in_mcp_as_that_user(oauth_live, google):
    _, tokens = d.tokens_for(oauth_live)
    async with oauth_live.client(str(tokens["access_token"])) as alice:
        saved = await alice.call_tool(
            "kb_remember", {"title": "Via OAuth", "body": "signed in with Google"}
        )
        item = await alice.call_tool("kb_get", {"id": saved.structured_content["id"]})
        assert item.structured_content["author"] == "alice@acme.com"
        assert item.structured_content["scope"] == "private:alice@acme.com"
    google.account = {
        "email": "bob@acme.com",
        "email_verified": True,
        "hd": "acme.com",
        "sub": "g-bob",
    }
    _, bob_tokens = d.tokens_for(oauth_live)
    async with oauth_live.client(str(bob_tokens["access_token"])) as bob:
        found = await bob.call_tool(
            "kb_consult", {"query": "signed in google", "other_groups": "always"}
        )
        assert (
            found.structured_content["results"] == []
            and found.structured_content["from_other_groups"] == []
        )


async def test_static_tokens_keep_working_next_to_oauth(oauth_live):
    token = oauth_live.token_for(Principal("svc", "acme", Role.MEMBER))
    assert mcp_status(oauth_live, token) == 200


def test_the_upstream_login_is_hardened(oauth_live, google):
    d.tokens_for(oauth_live)
    request = google.authorize_requests[-1]
    assert request["code_challenge_method"] == "S256" and len(request["code_challenge"]) == 43
    assert len(request["nonce"]) >= 40 and len(request["state"]) >= 40
    assert request["scope"] == "openid email" and request["hd"] == "acme.com"
    assert request["redirect_uri"] == f"{oauth_live.url}/oauth/google/callback"


# ---------------------------------------------------------------- consent
def test_consent_page_shows_who_is_asking_and_resists_injection(oauth_live):
    client_id = d.register(oauth_live, name="<img src=x onerror=alert(1)>").json()["client_id"]
    _, challenge = d.pkce()
    with httpx.Client() as browser:
        stage = d.follow_to_consent(
            oauth_live, browser, d.authorize_url(oauth_live, client_id, challenge)
        )
    page = stage.page
    assert page.status_code == 200 and "Allow access?" in page.text
    assert "<img src=x" not in page.text and "&lt;img src=x" in page.text
    assert "alice@acme.com" in page.text and "https://client.example" in page.text
    assert page.headers["x-frame-options"] == "DENY" and page.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "<script" not in page.text.lower()


def test_denying_returns_access_denied_with_the_clients_state(oauth_live):
    _, _, result = login(oauth_live, approve=False, state="keep-me")
    assert result.code is None and d.query(result.final_url) == {
        "error": "access_denied",
        "state": "keep-me",
    }


def test_consent_cannot_be_replayed_or_forged(oauth_live):
    client_id = d.register(oauth_live).json()["client_id"]
    _, challenge = d.pkce()
    with httpx.Client() as browser:
        stage = d.follow_to_consent(
            oauth_live, browser, d.authorize_url(oauth_live, client_id, challenge)
        )
        txn = re.search(r"name=txn value='([^']+)'", stage.page.text).group(1)
        csrf = re.search(r"name=csrf value='([^']+)'", stage.page.text).group(1)
        url = f"{oauth_live.url}/oauth/consent"
        assert (
            browser.post(url, data={"txn": txn, "csrf": "wrong", "decision": "approve"}).status_code
            == 403
        )
        assert (
            httpx.post(url, data={"txn": txn, "csrf": csrf, "decision": "approve"}).status_code
            == 403
        )  # no cookie
        assert httpx.get(f"{url}?txn={txn}").status_code == 403
        ok = browser.post(url, data={"txn": txn, "csrf": csrf, "decision": "approve"})
        assert ok.status_code == 303 and "code=" in ok.headers["location"]
        again = browser.post(url, data={"txn": txn, "csrf": csrf, "decision": "approve"})
        assert again.status_code in (400, 403)  # the transaction was consumed


# ---------------------------------------------------------------- login CSRF
def test_callback_requires_the_browser_that_started_the_login(oauth_live, google):
    client_id = d.register(oauth_live).json()["client_id"]
    _, challenge = d.pkce()
    with httpx.Client() as victim, httpx.Client() as attacker:
        # The attacker starts a login and obtains a valid callback URL, then lures the victim to open it.
        stage = attacker.get(d.authorize_url(oauth_live, client_id, challenge))
        start = attacker.get(stage.headers["location"])
        callback = attacker.get(start.headers["location"]).headers["location"]
        assert (
            victim.get(callback).status_code == 400
        )  # victim's browser has no cookie for this login
        assert "not started in this browser" in victim.get(callback).text
        other_start = victim.get(
            attacker.get(d.authorize_url(oauth_live, client_id, challenge)).headers["location"]
        )
        assert other_start.status_code == 302  # victim now holds a cookie for a *different* login
        assert (
            victim.get(callback).status_code == 400
        )  # which does not unlock the attacker's transaction


def test_malformed_requests_get_clean_errors_not_server_errors(oauth_live):
    for path in (
        "/oauth/start",
        "/oauth/start?txn=nonsense",
        "/oauth/google/callback",
        "/oauth/google/callback?code=x&state=y",
        "/oauth/consent",
        "/oauth/consent?txn=zzz",
    ):
        status = httpx.get(f"{oauth_live.url}{path}").status_code
        assert status in (400, 403), (path, status)
    assert httpx.post(
        f"{oauth_live.url}/oauth/consent", data={"decision": "approve"}
    ).status_code in (400, 403)


# ---------------------------------------------------------------- who may sign in
@pytest.mark.parametrize(
    "account",
    [
        {
            "email": "stranger@acme.com",
            "email_verified": True,
            "hd": "acme.com",
            "sub": "g-x",
        },  # not in the directory
        {
            "email": "alice@evil.com",
            "email_verified": True,
            "hd": "evil.com",
            "sub": "g-x",
        },  # wrong domain
        {"email": "alice@acme.com", "email_verified": False, "hd": "acme.com", "sub": "g-x"},
        {
            "email": "alice@acme.com",
            "email_verified": True,
            "hd": None,
            "sub": "g-x",
        },  # personal account
    ],
)
def test_refused_users_never_see_a_consent_page_and_the_client_only_sees_access_denied(
    oauth_live, google, account
):
    google.account = account
    _, _, result = login(oauth_live)
    assert result.code is None and d.query(result.final_url)["error"] == "access_denied"
    assert result.page is None or "Allow access?" not in result.page.text


def test_a_disabled_user_cannot_start_a_login(oauth_live):
    oauth_live.directory.upsert(DirectoryUser("alice@acme.com", "acme", Role.MEMBER, disabled=True))
    assert login(oauth_live)[2].code is None


# ---------------------------------------------------------------- forged or wrong Google tokens
def forged_by_other_key(google):
    google.signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.mark.parametrize(
    "tamper",
    [
        forged_by_other_key,
        lambda g: g.claim_overrides.update(aud="someone-elses-client"),
        lambda g: g.claim_overrides.update(iss="https://evil.example"),
        lambda g: g.claim_overrides.update(nonce="replayed-nonce"),
        lambda g: g.claim_overrides.update(
            exp=int(time.time()) - 3600, iat=int(time.time()) - 7200
        ),
        lambda g: g.claim_overrides.update(email=None),
        lambda g: g.header_overrides.update(alg="HS256"),
        lambda g: g.header_overrides.update(alg="none"),
        lambda g: g.header_overrides.update(kid="unknown-key"),
    ],
)
def test_unverifiable_google_tokens_never_produce_a_code(oauth_live, google, tamper):
    tamper(google)
    _, _, result = login(oauth_live)
    assert result.code is None
    assert (
        result.page is not None
        and result.page.status_code == 502
        and "could not be verified" in result.page.text
    )


def test_google_refusing_the_login_is_reported_as_access_denied(oauth_live, google):
    google.deny_login = True
    _, _, result = login(oauth_live)
    assert result.code is None and d.query(result.final_url)["error"] == "access_denied"


# ---------------------------------------------------------------- the token endpoint
def test_pkce_is_enforced_at_the_token_endpoint(oauth_live):
    client_id, verifier, result = login(oauth_live)
    wrong = d.exchange(oauth_live, client_id, result.code, "w" * 64)
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant"
    missing = d.token_request(
        oauth_live,
        grant_type="authorization_code",
        client_id=client_id,
        code=result.code,
        redirect_uri=d.REDIRECT,
    )
    assert missing.status_code == 400
    assert d.exchange(oauth_live, client_id, result.code, verifier).status_code == 200


def test_redirect_uri_must_match_exactly(oauth_live):
    client_id, verifier, result = login(oauth_live)
    assert (
        d.exchange(
            oauth_live, client_id, result.code, verifier, "https://client.example/other"
        ).status_code
        == 400
    )
    unregistered = httpx.get(
        d.authorize_url(oauth_live, client_id, d.pkce()[1], redirect_uri="https://evil.example/cb")
    )
    assert (
        unregistered.status_code == 400 and "location" not in unregistered.headers
    )  # never redirected there


def test_a_code_cannot_be_used_by_another_client(oauth_live):
    _, verifier, result = login(oauth_live)
    other = d.register(oauth_live, name="other").json()["client_id"]
    assert d.exchange(oauth_live, other, result.code, verifier).status_code == 400


def test_replaying_a_code_fails_and_revokes_the_token_it_produced(oauth_live):
    client_id, verifier, result = login(oauth_live)
    first = d.exchange(oauth_live, client_id, result.code, verifier)
    assert first.status_code == 200 and mcp_status(oauth_live, first.json()["access_token"]) == 200
    assert d.exchange(oauth_live, client_id, result.code, verifier).status_code == 400
    assert (
        mcp_status(oauth_live, first.json()["access_token"]) == 401
    )  # the leaked-code scenario is contained


def test_refresh_tokens_rotate_and_theft_is_detected(oauth_live):
    client_id, tokens = d.tokens_for(oauth_live)
    second = d.refresh(oauth_live, client_id, str(tokens["refresh_token"]))
    assert second.status_code == 200 and second.json()["refresh_token"] != tokens["refresh_token"]
    assert mcp_status(oauth_live, second.json()["access_token"]) == 200
    stolen = d.refresh(
        oauth_live, client_id, str(tokens["refresh_token"])
    )  # the old token comes back
    assert stolen.status_code == 400
    assert (
        mcp_status(oauth_live, second.json()["access_token"]) == 401
    )  # the whole session is revoked
    assert d.refresh(oauth_live, client_id, second.json()["refresh_token"]).status_code == 400


def test_other_resources_and_scopes_are_refused(oauth_live):
    client_id = d.register(oauth_live).json()["client_id"]
    _, challenge = d.pkce()
    with httpx.Client() as browser:
        wrong_resource = d.follow_to_consent(
            oauth_live,
            browser,
            d.authorize_url(oauth_live, client_id, challenge, resource="https://other.example/mcp"),
        )
        wrong_scope = d.follow_to_consent(
            oauth_live,
            browser,
            d.authorize_url(oauth_live, client_id, challenge, scope="hive admin"),
        )
    assert d.query(wrong_resource.final_url)["error"] == "invalid_target"
    assert d.query(wrong_scope.final_url)["error"] == "invalid_scope"


def revoke(live, client_id, token, **extra):
    return httpx.post(f"{live.url}/revoke", data={"token": token, "client_id": client_id, **extra})


def test_revocation_ends_the_whole_session_for_a_public_client(oauth_live):
    client_id, tokens = d.tokens_for(
        oauth_live
    )  # a PKCE client with no secret, like nearly every MCP client
    assert mcp_status(oauth_live, str(tokens["access_token"])) == 200
    assert revoke(oauth_live, client_id, tokens["access_token"]).status_code == 200
    assert mcp_status(oauth_live, str(tokens["access_token"])) == 401
    assert d.refresh(oauth_live, client_id, str(tokens["refresh_token"])).status_code == 400
    again_id, again = d.tokens_for(oauth_live)
    assert (
        revoke(
            oauth_live, again_id, again["refresh_token"], token_type_hint="refresh_token"
        ).status_code
        == 200
    )
    assert mcp_status(oauth_live, str(again["access_token"])) == 401


def test_revocation_cannot_touch_other_clients_or_static_tokens_and_never_leaks(oauth_live):
    client_id, tokens = d.tokens_for(oauth_live)
    intruder = d.register(oauth_live, name="intruder").json()["client_id"]
    assert (
        revoke(oauth_live, intruder, tokens["access_token"]).status_code == 200
    )  # same answer as success
    assert mcp_status(oauth_live, str(tokens["access_token"])) == 200  # but nothing was revoked
    static = oauth_live.token_for(Principal("svc", "acme", Role.MEMBER))
    assert (
        revoke(oauth_live, client_id, static).status_code == 200
        and mcp_status(oauth_live, static) == 200
    )
    assert (
        revoke(oauth_live, client_id, "hat_not-a-token").status_code == 200
    )  # RFC 7009: unknown is fine
    assert (
        httpx.post(f"{oauth_live.url}/revoke", data={"token": "x"}).status_code == 401
    )  # no client_id
    assert revoke(oauth_live, "no-such-client", "x").status_code == 401
    assert (
        httpx.post(f"{oauth_live.url}/revoke", data={"client_id": client_id}).status_code == 400
    )  # no token
    assert (
        httpx.post(
            f"{oauth_live.url}/revoke",
            content=b"x" * 70_000,
            headers={"content-type": "application/x-www-form-urlencoded"},
        ).status_code
        == 413
    )


def test_a_confidential_client_must_prove_itself_to_revoke(oauth_live):
    secret_client = d.register(oauth_live, token_endpoint_auth_method="client_secret_post").json()
    verifier, challenge = d.pkce()
    result = d.full_login(oauth_live, secret_client["client_id"], challenge)
    response = d.token_request(
        oauth_live,
        grant_type="authorization_code",
        client_id=secret_client["client_id"],
        code=result.code,
        code_verifier=verifier,
        redirect_uri=d.REDIRECT,
        client_secret=secret_client["client_secret"],
    )
    token = response.json()["access_token"]
    assert (
        revoke(oauth_live, secret_client["client_id"], token, client_secret="wrong").status_code
        == 401
    )
    assert mcp_status(oauth_live, token) == 200
    assert (
        revoke(
            oauth_live,
            secret_client["client_id"],
            token,
            client_secret=secret_client["client_secret"],
        ).status_code
        == 200
    )
    assert mcp_status(oauth_live, token) == 401


# ---------------------------------------------------------------- directory changes reach live tokens
async def test_removing_or_demoting_a_user_takes_effect_on_the_next_request(oauth_live):
    _, tokens = d.tokens_for(oauth_live)
    token = str(tokens["access_token"])
    async with oauth_live.client(token) as client:
        assert not (
            await client.call_tool("kb_remember", {"title": "Allowed", "body": "member can write"})
        ).is_error
    oauth_live.directory.upsert(
        DirectoryUser("alice@acme.com", "acme", Role.VIEWER, projects=frozenset({"p1"}))
    )
    async with oauth_live.client(token) as client:
        denied = await client.call_tool(
            "kb_remember", {"title": "Now denied", "body": "viewer is read-only"}
        )
        assert denied.is_error and "cannot create" in denied.content[0].text
        assert not (await client.call_tool("kb_consult", {"query": "member can write"})).is_error
    oauth_live.directory.remove("alice@acme.com")
    assert mcp_status(oauth_live, token) == 401


# ---------------------------------------------------------------- client registration
def test_dynamic_registration_refuses_dangerous_redirects_and_accepts_native_ones(oauth_live):
    for bad in ("javascript:alert(1)", "http://evil.example/cb", "data:text/html,x"):
        response = d.register(oauth_live, redirect_uris=[bad])
        assert response.status_code == 400 and response.json()["error"] in (
            "invalid_redirect_uri",
            "invalid_client_metadata",
        )
    for good in (
        "http://localhost:7777/cb",
        "http://127.0.0.1/cb",
        "cursor://anysphere.cursor/oauth",
    ):
        assert d.register(oauth_live, redirect_uris=[good]).status_code == 201
    assert d.register(oauth_live, redirect_uris=[]).status_code == 400
    assert d.register(oauth_live, scope="hive admin").status_code == 400


# ---------------------------------------------------------------- remembered consent
def test_approval_is_remembered_for_the_same_user_client_and_redirect(oauth_live):
    client_id = d.register(oauth_live).json()["client_id"]
    verifier, challenge = d.pkce()
    with httpx.Client() as browser:  # first time: the approval page is shown
        stage = d.follow_to_consent(
            oauth_live, browser, d.authorize_url(oauth_live, client_id, challenge)
        )
        assert "Allow access?" in stage.page.text
        assert d.decide(oauth_live, browser, stage.page).code
    verifier2, challenge2 = d.pkce()
    with httpx.Client() as browser:  # second time: straight back to the client with a fresh code
        again = d.follow_to_consent(
            oauth_live, browser, d.authorize_url(oauth_live, client_id, challenge2)
        )
    assert again.page is None and again.code
    assert d.exchange(oauth_live, client_id, again.code, verifier2).status_code == 200
    assert verifier != verifier2


def asks(live, client_id, **kw):
    """Does a fresh authorize request show the approval page (True) or go straight back (False)?"""
    _, challenge = d.pkce()
    with httpx.Client() as browser:
        stage = d.follow_to_consent(
            live, browser, d.authorize_url(live, client_id, challenge, **kw)
        )
    return stage.page is not None and "Allow access?" in stage.page.text


def test_remembered_approval_does_not_carry_over_to_other_clients_users_or_redirects(
    oauth_live, google
):
    other_redirect = "https://client.example/other"
    client_id = d.register(oauth_live, redirect_uris=[d.REDIRECT, other_redirect]).json()[
        "client_id"
    ]
    _, challenge = d.pkce()
    assert d.full_login(oauth_live, client_id, challenge).code
    assert not asks(oauth_live, client_id)  # remembered for this client + redirect
    assert asks(oauth_live, client_id, redirect_uri=other_redirect)  # other redirect: asked
    assert asks(oauth_live, d.register(oauth_live).json()["client_id"])  # other client: asked
    google.account = {"email": "bob@acme.com", "email_verified": True, "hd": "acme.com", "sub": "b"}
    assert asks(oauth_live, client_id)  # other user: asked


def test_denial_is_not_remembered(oauth_live):
    client_id = d.register(oauth_live).json()["client_id"]
    _, challenge = d.pkce()
    assert d.full_login(oauth_live, client_id, challenge, approve=False).error == "access_denied"
    with httpx.Client() as browser:
        stage = d.follow_to_consent(
            oauth_live, browser, d.authorize_url(oauth_live, client_id, challenge)
        )
    assert stage.page is not None and "Allow access?" in stage.page.text


def test_memory_can_be_switched_off(oauth_live_no_memory):
    client_id = d.register(oauth_live_no_memory).json()["client_id"]
    _, challenge = d.pkce()
    assert d.full_login(oauth_live_no_memory, client_id, challenge).code
    with httpx.Client() as browser:
        stage = d.follow_to_consent(
            oauth_live_no_memory,
            browser,
            d.authorize_url(oauth_live_no_memory, client_id, challenge),
        )
    assert stage.page is not None and "Allow access?" in stage.page.text


# ---------------------------------------------------------------- abuse limits
def test_client_registration_is_rate_limited_per_address(tmp_path, google):
    live = _oauth_server(tmp_path, google, rate_limit_register=3)
    try:
        codes = [d.register(live).status_code for _ in range(5)]
        assert codes[:3] == [201] * 3 and codes[3:] == [429, 429]
        blocked = d.register(live)
        assert int(blocked.headers["retry-after"]) >= 1
        assert httpx.get(f"{live.url}/healthz").status_code == 200  # health is never limited
        assert httpx.get(f"{live.url}/.well-known/oauth-authorization-server").status_code == 200
    finally:
        live.stop()


def test_mcp_requests_are_limited_per_token_not_globally(tmp_path):
    live = Live(tmp_path / "data", rate_limit_mcp=60)  # burst of 10
    live.start()
    try:
        a = live.token_for(Principal("a", "org1", Role.MEMBER))
        b = live.token_for(Principal("b", "org1", Role.MEMBER))
        statuses = [mcp_status(live, a) for _ in range(14)]
        assert statuses.count(200) == 10 and statuses.count(429) == 4
        assert mcp_status(live, b) == 200  # another agent is unaffected
    finally:
        live.stop()
