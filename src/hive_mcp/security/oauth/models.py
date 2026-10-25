"""OAuth constants, stored-record models and the token digest."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken

SCOPE = "hive"
NS_CLIENT = "oauth:client"
NS_TXN = "oauth:txn"  # a login in progress, bound to the browser by a signed cookie
NS_CODE = "oauth:code"
NS_CODE_USED = "oauth:code-used"  # remembers spent codes so a replay can revoke what they produced
NS_CONSENT = "oauth:consent"  # "remember my approval" per user + client + redirect + scopes
NS_ACCESS = "oauth:access"
NS_REFRESH = "oauth:refresh"
NS_REFRESH_USED = "oauth:refresh-used"  # remembers rotated refresh tokens to detect theft
NS_FAMILY = (
    "oauth:family"  # all tokens descended from one authorization; revoking it kills them all
)

ACCESS_PREFIX = "hat_"
REFRESH_PREFIX = "hrt_"
CLIENT_TTL_SECONDS = 30 * 86400  # registered clients that never complete a login expire
TXN_TTL_SECONDS = 600
CODE_TTL_SECONDS = 300


def digest(secret: str) -> str:
    """Records are keyed by a hash, so a database read never yields a usable token."""
    return hashlib.sha256(secret.encode()).hexdigest()


class HiveAuthCode(AuthorizationCode):
    user: str  # the directory user (email) who approved
    family: str


class HiveRefresh(RefreshToken):
    user: str
    family: str


class HiveAccess(AccessToken):
    family: str


@dataclass(frozen=True, slots=True)
class OAuthConfig:
    public_url: str
    resource_url: str
    access_ttl: int = 3600
    refresh_ttl: int = 30 * 86400
    consent_ttl: int = 0  # seconds an approval is remembered; 0 = ask every time
