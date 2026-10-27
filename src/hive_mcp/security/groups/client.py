"""Reads group members from the Google Admin SDK Directory API.

Authentication is a service account with domain-wide delegation, impersonating a Workspace admin
(read-only group scope). The JWT-bearer exchange is done with PyJWT, so no Google SDK is needed.
"""

from __future__ import annotations

from typing import Any

import httpx

from hive_mcp.security.google_auth import HTTP_TIMEOUT, ServiceAccountToken
from hive_mcp.security.oauth.upstream import UpstreamError

SCOPE = "https://www.googleapis.com/auth/admin.directory.group.member.readonly"
API_BASE = "https://admin.googleapis.com"
PAGE = 200
MAX_PAGES = 200  # 40,000 members per group; more means something is wrong


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
        self._http = http or httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        self._auth = ServiceAccountToken(
            key_info, admin_email, SCOPE, token_uri=token_uri, http=self._http
        )
        self._api = api_base.rstrip("/")

    async def _access_token(self) -> str:
        return await self._auth.get()

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
