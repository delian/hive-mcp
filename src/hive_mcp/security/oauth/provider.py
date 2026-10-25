"""OAuth authorization server provider: the storage and token rules behind the SDK's protocol handlers.

The SDK handles discovery, client registration, PKCE (S256), redirect-URI matching and code expiry. This
provider adds what it deliberately leaves out: persistence, single-use codes, refresh-token rotation with
reuse detection, resource binding, live directory checks, and the login/consent hand-off.
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AuthorizationParams,
    AuthorizeError,
    IdentityAssertionParams,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from hive_mcp.security.oauth.models import (
    ACCESS_PREFIX,
    CLIENT_TTL_SECONDS,
    CODE_TTL_SECONDS,
    NS_CLIENT,
    NS_CODE,
    NS_CODE_USED,
    NS_CONSENT,
    NS_FAMILY,
    NS_TXN,
    SCOPE,
    TXN_TTL_SECONDS,
    HiveAuthCode,
    HiveRefresh,
    OAuthConfig,
    digest,
)
from hive_mcp.security.oauth.tokens import TokenService

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.auth.provider import AccessToken, RefreshToken

    from hive_mcp.domain.clock import Clock
    from hive_mcp.ports.kv import JsonObject, KeyValueStore
    from hive_mcp.security.directory import UserDirectory
    from hive_mcp.security.tokens import StaticTokenVerifier

log = logging.getLogger("hive.security")
LOOPBACK = ("localhost", "127.0.0.1", "[::1]", "::1")
DANGEROUS_SCHEMES = frozenset({"javascript", "data", "file", "vbscript", "blob", "about", "ftp"})


def check_redirect_uris(uris: list[Any]) -> None:
    """https, loopback http (native apps) and app-specific schemes are fine; script-like schemes are not."""
    if not uris:
        raise RegistrationError("invalid_redirect_uri", "at least one redirect URI is required")
    for raw in uris:
        uri = urlparse(str(raw))
        scheme = uri.scheme.lower()
        if uri.fragment or scheme in DANGEROUS_SCHEMES or not scheme:
            raise RegistrationError(
                "invalid_redirect_uri", "redirect URI scheme or fragment not allowed"
            )
        if scheme == "http" and uri.hostname not in LOOPBACK:
            raise RegistrationError(
                "invalid_redirect_uri", "http redirect URIs are only allowed for loopback"
            )
        if scheme in ("http", "https") and not uri.hostname:
            raise RegistrationError("invalid_redirect_uri", "redirect URI needs a host")


class HiveOAuthProvider:
    def __init__(
        self,
        *,
        get_kv: Callable[[], KeyValueStore],
        directory: UserDirectory,
        config: OAuthConfig,
        clock: Clock,
        static_tokens: StaticTokenVerifier | None = None,
    ) -> None:
        self._kv, self.directory, self.config = get_kv, directory, config
        self._clock, self._static = clock, static_tokens
        self.tokens = TokenService(get_kv, directory, config, clock)

    @property
    def kv(self) -> KeyValueStore:
        return self._kv()

    def _now(self) -> int:
        return self.tokens.now()

    # ---- clients (RFC 7591 dynamic registration is handled by the SDK; we persist) ----
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        record = await self.kv.get(NS_CLIENT, client_id)
        return OAuthClientInformationFull.model_validate(record) if record else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        check_redirect_uris(list(client_info.redirect_uris or []))
        stored = await self.kv.put_if_absent(
            NS_CLIENT,
            str(client_info.client_id),
            client_info.model_dump(mode="json"),
            ttl_seconds=CLIENT_TTL_SECONDS,
        )
        if not stored:
            raise RegistrationError("invalid_client_metadata", "client id already registered")

    # ---- authorization: hand the browser to our login page, which ends in consent ----
    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource and params.resource != self.config.resource_url:
            raise AuthorizeError("invalid_target", "this server only issues tokens for itself")
        scopes = params.scopes or [SCOPE]
        if set(scopes) - {SCOPE}:
            raise AuthorizeError("invalid_scope", f"only the '{SCOPE}' scope exists")
        txn = secrets.token_urlsafe(32)
        await self.kv.put(
            NS_TXN,
            txn,
            {
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "redirect_explicit": params.redirect_uri_provided_explicitly,
                "state": params.state,
                "scopes": scopes,
                "code_challenge": params.code_challenge,
                "resource": self.config.resource_url,
                "nonce": secrets.token_urlsafe(32),
                "upstream_verifier": secrets.token_urlsafe(48),
                "csrf": secrets.token_urlsafe(32),
                "user": None,
            },
            ttl_seconds=TXN_TTL_SECONDS,
        )
        return f"{self.config.public_url.rstrip('/')}/oauth/start?txn={txn}"

    # ---- login transaction (used by the HTTP routes) ----
    async def load_txn(self, txn_id: str) -> JsonObject | None:
        return await self.kv.get(NS_TXN, txn_id)

    async def attach_user(self, txn_id: str, txn: JsonObject, email: str) -> None:
        await self.kv.put(NS_TXN, txn_id, {**txn, "user": email}, ttl_seconds=TXN_TTL_SECONDS)

    @staticmethod
    def _consent_key(txn: JsonObject) -> str:
        """Approval is bound to the exact user, client, redirect target and scopes."""
        parts = [txn["user"], txn["client_id"], txn["redirect_uri"], *sorted(txn["scopes"])]
        return digest(json.dumps(parts))

    async def has_consent(self, txn: JsonObject) -> bool:
        if self.config.consent_ttl <= 0 or not txn.get("user"):
            return False
        return await self.kv.get(NS_CONSENT, self._consent_key(txn)) is not None

    async def remember_consent(self, txn: JsonObject) -> None:
        if self.config.consent_ttl > 0 and txn.get("user"):
            await self.kv.put(
                NS_CONSENT, self._consent_key(txn), {"user": txn["user"]},
                ttl_seconds=self.config.consent_ttl,
            )  # fmt: skip

    async def finish(
        self, txn_id: str, *, approve: bool, error: str = "access_denied"
    ) -> str | None:
        """Consume the transaction (once) and return the URL to send the browser back to the client."""
        txn = await self.kv.take(NS_TXN, txn_id)
        if txn is None:
            return None
        base = txn["redirect_uri"]
        state = {"state": txn["state"]} if txn["state"] else {}
        if not approve or not txn["user"]:
            return construct_redirect_uri(base, error=error, **state)
        code = secrets.token_urlsafe(32)
        family = secrets.token_urlsafe(16)
        await self.kv.put(
            NS_CODE,
            digest(code),
            {
                "client_id": txn["client_id"],
                "scopes": txn["scopes"],
                "code_challenge": txn["code_challenge"],
                "redirect_uri": base,
                "redirect_explicit": txn["redirect_explicit"],
                "resource": txn["resource"],
                "user": txn["user"],
                "family": family,
                "expires_at": self._now() + CODE_TTL_SECONDS,
            },
            ttl_seconds=CODE_TTL_SECONDS,
        )
        await self.kv.put(
            NS_FAMILY,
            family,
            {"revoked": False, "user": txn["user"]},
            ttl_seconds=self.config.refresh_ttl,
        )
        log.info("oauth.code_issued user=%s client=%s", txn["user"], txn["client_id"])
        return construct_redirect_uri(base, code=code, **state)

    # ---- authorization codes ----
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> HiveAuthCode | None:
        key = digest(authorization_code)
        record = await self.kv.get(NS_CODE, key)
        if record is None:
            spent = await self.kv.get(NS_CODE_USED, key)
            if spent:  # RFC 6749 4.1.2: a replayed code means it leaked; kill what it produced
                await self.tokens.revoke_family(spent["family"])
                log.warning(
                    "oauth.code_replay client=%s family=%s", client.client_id, spent["family"]
                )
            return None
        if record["client_id"] != client.client_id:
            return None
        return HiveAuthCode(
            code=authorization_code,
            scopes=record["scopes"],
            expires_at=record["expires_at"],
            client_id=record["client_id"],
            code_challenge=record["code_challenge"],
            redirect_uri=record["redirect_uri"],
            redirect_uri_provided_explicitly=record["redirect_explicit"],
            resource=record["resource"],
            subject=record["user"],
            user=record["user"],
            family=record["family"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: HiveAuthCode
    ) -> OAuthToken:
        key = digest(authorization_code.code)
        if await self.kv.take(NS_CODE, key) is None:  # lost a race with another exchange: replay
            await self.tokens.revoke_family(authorization_code.family)
            raise TokenError("invalid_grant", "authorization code already used")
        await self.kv.put(
            NS_CODE_USED,
            key,
            {"family": authorization_code.family},
            ttl_seconds=CODE_TTL_SECONDS * 2,
        )
        self.tokens.require_live_user(authorization_code.user)
        return await self.tokens.issue(
            authorization_code.user,
            client.client_id or "",
            authorization_code.scopes,
            authorization_code.resource,
            authorization_code.family,
        )

    # ---- refresh tokens and access tokens: see TokenService ----
    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> HiveRefresh | None:
        return await self.tokens.load_refresh(client, refresh_token)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: HiveRefresh, scopes: list[str]
    ) -> OAuthToken:
        return await self.tokens.rotate(client, refresh_token, scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if token.startswith(ACCESS_PREFIX):
            return await self.tokens.load_access(token)
        return await self._static.verify_token(token) if self._static else None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        family = getattr(token, "family", None)
        if family:
            await self.tokens.revoke_family(family)

    async def exchange_identity_assertion(
        self, client: OAuthClientInformationFull, params: IdentityAssertionParams
    ) -> OAuthToken:
        """The enterprise ID-JAG grant is not offered (and the SDK does not advertise it)."""
        raise TokenError("unsupported_grant_type", "the JWT bearer grant is not supported")
