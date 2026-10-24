"""Access/refresh token lifecycle: issuing, rotation with reuse detection, families, live user checks."""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING

from mcp.server.auth.provider import TokenError
from mcp.shared.auth import OAuthToken

from hive_mcp.security.oauth.models import (
    ACCESS_PREFIX,
    NS_ACCESS,
    NS_FAMILY,
    NS_REFRESH,
    NS_REFRESH_USED,
    REFRESH_PREFIX,
    HiveAccess,
    HiveRefresh,
    OAuthConfig,
    digest,
)
from hive_mcp.security.principals import claims_for

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.shared.auth import OAuthClientInformationFull

    from hive_mcp.domain.clock import Clock
    from hive_mcp.ports.kv import KeyValueStore
    from hive_mcp.security.directory import UserDirectory

log = logging.getLogger("hive.security")


class TokenService:
    def __init__(
        self,
        get_kv: Callable[[], KeyValueStore],
        directory: UserDirectory,
        config: OAuthConfig,
        clock: Clock,
    ) -> None:
        self._kv, self._directory, self._config, self._clock = get_kv, directory, config, clock

    @property
    def kv(self) -> KeyValueStore:
        return self._kv()

    def now(self) -> int:
        return int(self._clock.now().timestamp())

    # ---- families: every token descends from one authorization; revoking the family kills them all ----
    async def start_family(self, family: str, user: str) -> None:
        await self.kv.put(
            NS_FAMILY,
            family,
            {"revoked": False, "user": user},
            ttl_seconds=self._config.refresh_ttl,
        )

    async def revoked(self, family: str) -> bool:
        record = await self.kv.get(NS_FAMILY, family)
        return record is None or bool(record.get("revoked"))

    async def revoke_family(self, family: str) -> None:
        record = await self.kv.get(NS_FAMILY, family) or {}
        await self.kv.put(
            NS_FAMILY, family, {**record, "revoked": True}, ttl_seconds=self._config.refresh_ttl
        )

    def require_live_user(self, email: str) -> None:
        if self._directory.lookup(email) is None:
            raise TokenError("invalid_grant", "user is no longer permitted")

    # ---- issuing ----
    async def issue(
        self, user: str, client_id: str, scopes: list[str], resource: str | None, family: str
    ) -> OAuthToken:
        access, refresh = (
            ACCESS_PREFIX + secrets.token_urlsafe(32),
            REFRESH_PREFIX + secrets.token_urlsafe(32),
        )
        now, ttl = self.now(), self._config.access_ttl
        base = {
            "client_id": client_id,
            "user": user,
            "scopes": scopes,
            "resource": resource,
            "family": family,
        }
        await self.kv.put(
            NS_ACCESS, digest(access), {**base, "expires_at": now + ttl}, ttl_seconds=ttl
        )
        await self.kv.put(
            NS_REFRESH,
            digest(refresh),
            {**base, "expires_at": now + self._config.refresh_ttl},
            ttl_seconds=self._config.refresh_ttl,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",  # noqa: S106 - token type, not a secret
            expires_in=ttl,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    # ---- access tokens ----
    async def load_access(self, token: str) -> HiveAccess | None:
        record = await self.kv.get(NS_ACCESS, digest(token))
        if record is None or await self.revoked(record["family"]):
            return None
        user = self._directory.lookup(
            record["user"]
        )  # live: removing or disabling a user cuts them off
        if user is None:
            return None
        return HiveAccess(
            token=token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=record["expires_at"],
            resource=record["resource"],
            subject=user.email,
            claims=claims_for(user.principal()) | {"iss": self._config.public_url},
            family=record["family"],
        )

    # ---- refresh tokens: rotating, with reuse detection ----
    async def load_refresh(
        self, client: OAuthClientInformationFull, token: str
    ) -> HiveRefresh | None:
        key = digest(token)
        record = await self.kv.get(NS_REFRESH, key)
        if record is None:
            spent = await self.kv.get(NS_REFRESH_USED, key)
            if spent:  # an already-rotated token came back: someone holds a stolen copy
                await self.revoke_family(spent["family"])
                log.warning(
                    "oauth.refresh_reuse client=%s family=%s", client.client_id, spent["family"]
                )
            return None
        if record["client_id"] != client.client_id or await self.revoked(record["family"]):
            return None
        return HiveRefresh(
            token=token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=record["expires_at"],
            resource=record["resource"],
            subject=record["user"],
            user=record["user"],
            family=record["family"],
        )

    async def rotate(
        self, client: OAuthClientInformationFull, refresh: HiveRefresh, scopes: list[str]
    ) -> OAuthToken:
        if set(scopes) - set(refresh.scopes):
            raise TokenError("invalid_scope", "cannot widen the scope of a refresh token")
        key = digest(refresh.token)
        if await self.kv.take(NS_REFRESH, key) is None:  # concurrent use of the same token
            await self.revoke_family(refresh.family)
            raise TokenError("invalid_grant", "refresh token already used")
        await self.kv.put(
            NS_REFRESH_USED, key, {"family": refresh.family}, ttl_seconds=self._config.refresh_ttl
        )
        self.require_live_user(refresh.user)
        return await self.issue(
            refresh.user,
            client.client_id or "",
            scopes or refresh.scopes,
            refresh.resource,
            refresh.family,
        )
