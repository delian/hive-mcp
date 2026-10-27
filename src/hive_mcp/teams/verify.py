"""Checking that a request really comes from the Bot Framework (and so from Teams).

Every activity arrives with a bearer JWT signed by the Bot Framework. We check the signature
against its published keys, the issuer, our bot's application id as audience, the expiry, and that
the `serviceurl` claim equals the activity's `serviceUrl`: that URL is where replies (carrying our
bot token) are sent, so it must be the one the signature vouches for.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from hive_mcp.security.oauth.upstream import UpstreamError

OPENID_CONFIG = "https://login.botframework.com/v1/.well-known/openidconfiguration"
ISSUER = "https://api.botframework.com"
KEYS_TTL = 3600.0
REFETCH_MIN = 60.0
HTTP_TIMEOUT = 10.0

if TYPE_CHECKING:
    from collections.abc import Callable


class BotFrameworkVerifier:
    def __init__(
        self,
        app_id: str,
        *,
        http: httpx.AsyncClient | None = None,
        openid_config: str = OPENID_CONFIG,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._app_id, self._config_url, self._now = app_id, openid_config, now
        self._http = http or httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        self._keys: dict[str, Any] | None = None
        self._fetched = float("-inf")

    async def _load_keys(self) -> dict[str, Any]:
        try:
            config = (await self._http.get(self._config_url)).json()
            keys = (await self._http.get(str(config["jwks_uri"]))).json()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise UpstreamError("Bot Framework signing keys unavailable") from exc
        self._fetched = self._now()
        return {str(k["kid"]): k for k in keys.get("keys", []) if k.get("kty") == "RSA"}

    async def _key(self, kid: str | None) -> Any:  # noqa: ANN401 - library key object
        age = self._now() - self._fetched
        if self._keys is None or age > KEYS_TTL or (kid not in self._keys and age > REFETCH_MIN):
            self._keys = await self._load_keys()
        if kid not in self._keys:
            raise UpstreamError("token signed with an unknown key")
        return RSAAlgorithm.from_jwk(self._keys[str(kid)])

    async def verify(self, authorization: str | None, service_url: str) -> None:
        """Raises UpstreamError unless the header carries a valid token for this bot and URL."""
        if not authorization or authorization[:7].lower() != "bearer ":
            raise UpstreamError("missing bearer token")
        token = authorization[7:].strip()
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256":
                raise UpstreamError("unsupported token algorithm")
            claims = jwt.decode(
                token,
                key=await self._key(header.get("kid")),
                algorithms=["RS256"],
                audience=self._app_id,
                issuer=ISSUER,
                options={"require": ["exp", "iss", "aud"]},
                leeway=300,
            )
        except jwt.PyJWTError as exc:
            raise UpstreamError(f"token rejected: {exc.__class__.__name__}") from exc
        claimed = str(claims.get("serviceurl", "")).rstrip("/")
        if not claimed or claimed != service_url.rstrip("/"):
            raise UpstreamError("token does not vouch for this service url")
