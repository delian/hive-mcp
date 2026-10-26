"""Reads group members from the Google Admin SDK Directory API.

Authentication is a service account with domain-wide delegation, impersonating a Workspace admin
(read-only group scope). The JWT-bearer exchange is done with PyJWT, so no Google SDK is needed.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt

from hive_mcp.security.oauth.upstream import UpstreamError

SCOPE = "https://www.googleapis.com/auth/admin.directory.group.member.readonly"
TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - public endpoint
API_BASE = "https://admin.googleapis.com"
PAGE = 200
MAX_PAGES = 200  # 40,000 members per group; more means something is wrong
HTTP_TIMEOUT = 15.0


class GoogleGroups:
    def __init__(
        self,
        key_info: dict[str, Any],
        admin_email: str,
        *,
        api_base: str = API_BASE,
        token_uri: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        for field in ("client_email", "private_key"):
            if not isinstance(key_info.get(field), str):
                raise UpstreamError(f"service account key lacks {field}")
        self._key, self._admin = key_info, admin_email
        self._api = api_base.rstrip("/")
        self._token_uri = token_uri or str(key_info.get("token_uri") or TOKEN_URI)
        self._http = http or httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        self._token: tuple[str, float] | None = None

    async def _access_token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        now = int(time.time())
        claims = {
            "iss": self._key["client_email"],
            "sub": self._admin,
            "scope": SCOPE,
            "aud": self._token_uri,
            "iat": now,
            "exp": now + 3600,
        }
        assertion = jwt.encode(claims, self._key["private_key"], algorithm="RS256")
        try:
            response = await self._http.post(
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

    async def members(self, group_email: str) -> set[str]:
        """Active user members, including those who belong through nested groups (lower-cased)."""
        found: set[str] = set()
        page_token = ""
        for _ in range(MAX_PAGES):
            params = {"includeDerivedMembership": "true", "maxResults": str(PAGE)}
            if page_token:
                params["pageToken"] = page_token
            try:
                response = await self._http.get(
                    f"{self._api}/admin/directory/v1/groups/{group_email}/members",
                    params=params,
                    headers={"Authorization": f"Bearer {await self._access_token()}"},
                )
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise UpstreamError(f"could not read members of {group_email}") from exc
            for member in body.get("members", []):
                if member.get("type") == "USER" and member.get("status", "ACTIVE") == "ACTIVE":
                    found.add(str(member["email"]).casefold())
            page_token = body.get("nextPageToken", "")
            if not page_token:
                return found
        raise UpstreamError(f"{group_email} has implausibly many members")
