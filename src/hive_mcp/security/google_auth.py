"""Access tokens for a Google service account with domain-wide delegation (JWT-bearer grant).

Done with PyJWT, so no Google SDK is needed. The account impersonates one Workspace user and
asks for one read-only scope; tokens are cached until a minute before they expire.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt

from hive_mcp.security.oauth.upstream import UpstreamError

TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - public endpoint
HTTP_TIMEOUT = 15.0


class ServiceAccountToken:
    def __init__(
        self,
        key_info: dict[str, Any],
        subject: str,
        scope: str,
        *,
        token_uri: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        for field in ("client_email", "private_key"):
            if not isinstance(key_info.get(field), str):
                raise UpstreamError(f"service account key lacks {field}")
        self._key, self._subject, self._scope = key_info, subject, scope
        self._token_uri = token_uri or str(key_info.get("token_uri") or TOKEN_URI)
        self.http = http or httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        self._token: tuple[str, float] | None = None

    async def get(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        now = int(time.time())
        claims = {
            "iss": self._key["client_email"],
            "sub": self._subject,
            "scope": self._scope,
            "aud": self._token_uri,
            "iat": now,
            "exp": now + 3600,
        }
        assertion = jwt.encode(claims, self._key["private_key"], algorithm="RS256")
        try:
            response = await self.http.post(
                self._token_uri,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
            response.raise_for_status()
            body = response.json()
            self._token = (
                str(body["access_token"]),
                time.time() + float(body.get("expires_in", 3600)),
            )
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise UpstreamError(
                "Google refused the service account (check delegation and scope)"
            ) from exc
        return self._token[0]
