"""Google (OpenID Connect) upstream identity: login URL, code exchange, ID-token verification.

Signature and claims are verified with PyJWT even though the token arrives over a direct TLS call
(defence in depth). Endpoints come from the configured issuer's discovery document only.
"""

from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from hive_mcp.domain.errors import HiveError

GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
JWKS_MIN_REFETCH_SECONDS = 60.0
HTTP_TIMEOUT = 10.0


class UpstreamError(HiveError):
    """The identity provider failed or returned something we refuse to trust."""


@dataclass(frozen=True, slots=True)
class GoogleIdentity:
    email: str
    email_verified: bool
    hosted_domain: str | None
    subject: str


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class GoogleOidc:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        issuer: str,
        callback_url: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlparse(issuer)
        if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
            raise UpstreamError("the identity provider issuer must use https")
        self._id, self._secret, self._issuer = client_id, client_secret, issuer.rstrip("/")
        self._callback = callback_url
        self._http = http or httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        self._discovery: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None
        self._jwks_fetched = 0.0

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            response = await self._http.get(url)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamError("identity provider unreachable or invalid response") from exc
        if not isinstance(data, dict):
            raise UpstreamError("identity provider returned an unexpected document")
        return data

    async def discovery(self) -> dict[str, Any]:
        if self._discovery is None:
            doc = await self._get_json(f"{self._issuer}/.well-known/openid-configuration")
            if str(doc.get("issuer", "")).rstrip("/") != self._issuer:
                raise UpstreamError("discovery document issuer mismatch")
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                if not isinstance(doc.get(key), str):
                    raise UpstreamError(f"discovery document lacks {key}")
            self._discovery = doc
        return self._discovery

    async def authorization_url(
        self, *, state: str, nonce: str, code_verifier: str, hosted_domain: str | None
    ) -> str:
        doc = await self.discovery()
        params = {
            "client_id": self._id,
            "redirect_uri": self._callback,
            "response_type": "code",
            "scope": "openid email",
            "state": state,
            "nonce": nonce,
            "prompt": "select_account",
            "code_challenge": pkce_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
        if hosted_domain:
            params["hd"] = hosted_domain  # a UI hint only: the returned token is still checked
        return f"{doc['authorization_endpoint']}?{urlencode(params)}"

    async def _signing_key(self, kid: str | None) -> Any:  # noqa: ANN401 - library key object
        doc = await self.discovery()
        stale = time.monotonic() - self._jwks_fetched > JWKS_MIN_REFETCH_SECONDS
        if self._jwks is None or (stale and not self._has_key(kid)):
            self._jwks = await self._get_json(doc["jwks_uri"])
            self._jwks_fetched = time.monotonic()
        for key in (self._jwks or {}).get("keys", []):
            if key.get("kid") == kid and key.get("kty") == "RSA":
                return RSAAlgorithm.from_jwk(key)
        raise UpstreamError("ID token signed with an unknown key")

    def _has_key(self, kid: str | None) -> bool:
        return any(k.get("kid") == kid for k in (self._jwks or {}).get("keys", []))

    async def exchange(self, *, code: str, code_verifier: str, nonce: str) -> GoogleIdentity:
        """Trade the login code for an ID token and return the verified identity."""
        doc = await self.discovery()
        try:
            response = await self._http.post(
                doc["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self._callback,
                    "client_id": self._id,
                    "client_secret": self._secret,
                    "code_verifier": code_verifier,
                },
            )
            response.raise_for_status()
            id_token = response.json()["id_token"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise UpstreamError("code exchange with the identity provider failed") from exc
        return await self.verify_id_token(id_token, nonce)

    async def verify_id_token(self, id_token: str, nonce: str) -> GoogleIdentity:
        try:
            header = jwt.get_unverified_header(id_token)
            if header.get("alg") != "RS256":  # never trust the token to pick its own algorithm
                raise UpstreamError("unsupported ID token algorithm")
            key = await self._signing_key(header.get("kid"))
            claims = jwt.decode(
                id_token,
                key=key,
                algorithms=["RS256"],
                audience=self._id,
                issuer=list(GOOGLE_ISSUERS) if self._issuer == GOOGLE_ISSUERS[0] else self._issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
                leeway=60,
            )
        except jwt.PyJWTError as exc:
            raise UpstreamError(f"ID token rejected: {exc.__class__.__name__}") from exc
        if claims.get("nonce") != nonce:
            raise UpstreamError("ID token nonce mismatch")
        email = claims.get("email")
        if not isinstance(email, str) or not email:
            raise UpstreamError("ID token has no email")
        return GoogleIdentity(
            email=email,
            email_verified=claims.get("email_verified") is True,
            hosted_domain=claims.get("hd") if isinstance(claims.get("hd"), str) else None,
            subject=str(claims["sub"]),
        )
