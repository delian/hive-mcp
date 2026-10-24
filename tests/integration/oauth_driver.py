"""Plays the OAuth client and the user's browser against a live server, one explicit step at a time."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

from tests.integration.fake_google import b64

if TYPE_CHECKING:
    from tests.integration.conftest import Live

REDIRECT = "https://client.example/callback"


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, b64(hashlib.sha256(verifier.encode()).digest())


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


@dataclass
class Login:
    """The result of walking the browser through login + consent."""

    final_url: str | None = None
    page: httpx.Response | None = None  # the last page shown instead of a redirect to the client
    steps: list[str] = field(default_factory=list)

    @property
    def code(self) -> str | None:
        return query(self.final_url).get("code") if self.final_url else None

    @property
    def error(self) -> str | None:
        return query(self.final_url).get("error") if self.final_url else None


def register(
    live: Live, *, name: str = "Test App", redirect_uris: list[str] | None = None, **extra: object
) -> httpx.Response:
    body = {
        "client_name": name,
        "redirect_uris": [REDIRECT] if redirect_uris is None else redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        **extra,
    }
    return httpx.post(f"{live.url}/register", json=body)


def authorize_url(
    live: Live,
    client_id: str,
    challenge: str,
    *,
    state: str = "st-1",
    redirect_uri: str = REDIRECT,
    **extra: str,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        **extra,
    }
    return f"{live.url}/authorize?{urlencode(params)}"


def follow_to_consent(live: Live, browser: httpx.Client, url: str) -> Login:
    """GET authorize -> our /oauth/start -> Google -> our callback -> our consent page (or a final redirect)."""
    result = Login()
    for _ in range(10):
        response = browser.get(url)
        result.steps.append(f"{response.status_code} {urlparse(url).path}")
        location = response.headers.get("location")
        if location:
            location = urljoin(url, location)  # redirects may be relative
        if response.status_code in (301, 302, 303, 307) and location:
            if not location.startswith(live.url) and not location.startswith("http://127.0.0.1"):
                result.final_url = location  # left for the client's own redirect URI
                return result
            url = location
            continue
        result.page = response
        return result
    raise AssertionError("redirect loop")


def decide(
    live: Live, browser: httpx.Client, consent: httpx.Response, decision: str = "approve"
) -> Login:
    txn = re.search(r"name=txn value='([^']+)'", consent.text).group(1)  # type: ignore[union-attr]
    csrf = re.search(r"name=csrf value='([^']+)'", consent.text).group(1)  # type: ignore[union-attr]
    response = browser.post(
        f"{live.url}/oauth/consent", data={"txn": txn, "csrf": csrf, "decision": decision}
    )
    result = Login(page=response)
    if response.status_code == 303:
        result.final_url = response.headers["location"]
    return result


def full_login(
    live: Live, client_id: str, challenge: str, *, approve: bool = True, **kw: str
) -> Login:
    with httpx.Client(follow_redirects=False) as browser:
        stage = follow_to_consent(live, browser, authorize_url(live, client_id, challenge, **kw))
        if stage.final_url or stage.page is None or "Allow access?" not in stage.page.text:
            return stage
        return decide(live, browser, stage.page, "approve" if approve else "deny")


def token_request(live: Live, **form: str) -> httpx.Response:
    return httpx.post(f"{live.url}/token", data=form)


def exchange(
    live: Live, client_id: str, code: str, verifier: str, redirect_uri: str = REDIRECT
) -> httpx.Response:
    return token_request(
        live,
        grant_type="authorization_code",
        client_id=client_id,
        code=code,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
    )


def refresh(live: Live, client_id: str, refresh_token: str) -> httpx.Response:
    return token_request(
        live, grant_type="refresh_token", client_id=client_id, refresh_token=refresh_token
    )


def tokens_for(live: Live, **kw: str) -> tuple[str, dict[str, object]]:
    """Register a client, log in as the fake account, exchange the code; returns (client_id, token response)."""
    client_id = register(live).json()["client_id"]
    verifier, challenge = pkce()
    login = full_login(live, client_id, challenge, **kw)
    assert login.code, (
        f"login did not yield a code: {login.final_url} {login.page and login.page.text[:200]}"
    )
    response = exchange(live, client_id, login.code, verifier)
    assert response.status_code == 200, response.text
    return client_id, response.json()
