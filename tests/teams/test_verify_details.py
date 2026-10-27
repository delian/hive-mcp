"""Every check the Bot Framework token goes through, one at a time, with the exact refusal."""

from __future__ import annotations

import time

import httpx
import jwt
import pytest
from jwt.algorithms import RSAAlgorithm

from hive_mcp.security.oauth.upstream import UpstreamError
from hive_mcp.teams import verify as verify_module
from hive_mcp.teams.verify import BotFrameworkVerifier
from tests.teams.test_teams import APP, KEY, SERVICE, bot_token, keys_client


async def refused(v: BotFrameworkVerifier, header: str | None, url: str = SERVICE) -> str:
    with pytest.raises(UpstreamError) as caught:
        await v.verify(header, url)
    return str(caught.value)


async def test_each_refusal_says_what_was_wrong():
    v = BotFrameworkVerifier(APP, http=keys_client())
    assert await refused(v, None) == "missing bearer token"
    assert await refused(v, "Basic abc") == "missing bearer token"
    hs = jwt.encode({"exp": int(time.time()) + 60}, "k" * 32, algorithm="HS256")
    assert await refused(v, f"Bearer {hs}") == "unsupported token algorithm"
    unknown = jwt.encode({"exp": 1}, KEY, algorithm="RS256", headers={"kid": "nope"})
    assert await refused(v, f"Bearer {unknown}") == "token signed with an unknown key"
    expired = bot_token(exp=int(time.time()) - 4000)
    assert await refused(v, f"Bearer {expired}") == "token rejected: ExpiredSignatureError"
    assert await refused(v, f"Bearer {bot_token(aud='someone-else')}") == (
        "token rejected: InvalidAudienceError"
    )
    assert await refused(v, f"Bearer {bot_token(iss='https://evil')}") == (
        "token rejected: InvalidIssuerError"
    )
    assert await refused(v, f"Bearer {bot_token()}", "https://other.example") == (
        "token does not vouch for this service url"
    )


async def test_the_required_claims_are_really_required():
    v = BotFrameworkVerifier(APP, http=keys_client())
    for missing in ("exp", "iss", "aud"):
        claims = {"iss": verify_module.ISSUER, "aud": APP, "exp": int(time.time()) + 600}
        claims["serviceurl"] = SERVICE
        del claims[missing]
        token = jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"})
        assert await refused(v, f"Bearer {token}") == (
            "token rejected: MissingRequiredClaimError"
        ), missing


async def test_clock_skew_is_tolerated_up_to_exactly_five_minutes():
    v = BotFrameworkVerifier(APP, http=keys_client())
    await v.verify(f"Bearer {bot_token(exp=int(time.time()) - 290)}", SERVICE)
    assert "ExpiredSignatureError" in await refused(
        v, f"Bearer {bot_token(exp=int(time.time()) - 300)}"
    )  # 300 s ago is expired with a 300 s leeway; 301 would still pass


async def test_the_service_url_claim_must_exist_and_match_exactly_apart_from_a_slash():
    v = BotFrameworkVerifier(APP, http=keys_client())
    claims = {"iss": verify_module.ISSUER, "aud": APP, "exp": int(time.time()) + 600}
    bare = jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"})
    assert await refused(v, f"Bearer {bare}", "") == "token does not vouch for this service url"
    assert await refused(v, f"Bearer {bare}") == "token does not vouch for this service url"
    edge = bot_token(serviceurl="https://smba.trafficmanager.net/a/")
    await v.verify(f"Bearer {edge}", "https://smba.trafficmanager.net/a")
    await v.verify(f"Bearer {bot_token(serviceurl='https://h/a')}", "https://h/a//")
    odd = bot_token(serviceurl="https://h/aX")  # only slashes are ignored, not other characters
    assert await refused(v, f"Bearer {odd}", "https://h/a") == (
        "token does not vouch for this service url"
    )


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def counting_client(*, jwks: dict | None = None, fail: bool = False):
    calls: list[str] = []
    jwk = {**RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True), "kid": "k1"}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if fail:
            return httpx.Response(500, text="no")
        if request.url.path.endswith("openidconfiguration"):
            return httpx.Response(200, json={"jwks_uri": "https://login.test/keys"})
        return httpx.Response(200, json=jwks if jwks is not None else {"keys": [jwk]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


async def test_signing_keys_are_cached_for_an_hour_and_refetched_after():
    http, calls = counting_client()
    clock = Clock()
    v = BotFrameworkVerifier(APP, http=http, now=clock)
    header = f"Bearer {bot_token(exp=int(time.time()) + 100000)}"
    await v.verify(header, SERVICE)
    assert len(calls) == 2
    clock.t += verify_module.KEYS_TTL  # exactly an hour: still fresh
    await v.verify(header, SERVICE)
    assert len(calls) == 2
    clock.t += 1  # past it
    await v.verify(header, SERVICE)
    assert len(calls) == 4


async def test_a_known_key_never_triggers_a_refetch_and_an_unknown_one_only_after_a_minute():
    http, calls = counting_client()
    clock = Clock()
    v = BotFrameworkVerifier(APP, http=http, now=clock)
    good = f"Bearer {bot_token()}"
    await v.verify(good, SERVICE)
    clock.t += 1000  # an old cache, but the key is in it
    await v.verify(good, SERVICE)
    assert len(calls) == 2
    stranger = jwt.encode({"exp": 1}, KEY, algorithm="RS256", headers={"kid": "new-key"})
    clock.t = 1000 + verify_module.REFETCH_MIN  # keys were fetched at t=1000: exactly a minute
    await refused(v, f"Bearer {stranger}")
    assert len(calls) == 2  # too soon to ask again
    clock.t += 0.5
    await refused(v, f"Bearer {stranger}")
    assert len(calls) == 4  # an unknown key after a minute: the keys may have been rotated


async def test_unavailable_keys_and_a_key_list_without_keys_are_upstream_errors():
    down, _ = counting_client(fail=True)
    assert await refused(BotFrameworkVerifier(APP, http=down), f"Bearer {bot_token()}") == (
        "Bot Framework signing keys unavailable"
    )
    empty, _ = counting_client(jwks={})
    assert await refused(BotFrameworkVerifier(APP, http=empty), f"Bearer {bot_token()}") == (
        "token signed with an unknown key"
    )
    other, _ = counting_client(jwks={"keys": [{"kty": "oct", "kid": "k1"}]})  # not an RSA key
    assert await refused(BotFrameworkVerifier(APP, http=other), f"Bearer {bot_token()}") == (
        "token signed with an unknown key"
    )


async def test_a_missing_service_url_claim_never_matches_a_placeholder_url():
    v = BotFrameworkVerifier(APP, http=keys_client())
    claims = {"iss": verify_module.ISSUER, "aud": APP, "exp": int(time.time()) + 600}
    bare = jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"})
    for url in ("None", "XXXX", "()"):
        assert (
            await refused(v, f"Bearer {bare}", url) == "token does not vouch for this service url"
        )
    token = bot_token(serviceurl="https://h/a")
    assert await refused(v, f"Bearer {token}", "https://h/aX") == (
        "token does not vouch for this service url"
    )
