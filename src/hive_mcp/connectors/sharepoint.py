"""Connector for a SharePoint document library or OneDrive folder, through Microsoft Graph.

An Entra app registration with the *application* permission `Files.Read.All` (or
`Sites.Selected` for one site) reads with client credentials; the tenant admin grants it. The
drive item's eTag is the change marker, so unchanged files are not downloaded again.
"""

from __future__ import annotations

import time
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from hive_mcp.connectors.base import Document
from hive_mcp.domain.errors import ValidationError
from hive_mcp.ingest.git import READABLE
from hive_mcp.security.oauth.upstream import UpstreamError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"  # noqa: S105 - public URL
SCOPE = "https://graph.microsoft.com/.default"
MAX_FILES = 2000
MAX_DEPTH = 5
MAX_BYTES = 20 * 1024 * 1024


class SharePointConnector:
    def __init__(  # noqa: PLR0913 - the parts of one registration and one library
        self,
        tenant: str,
        client_id: str,
        client_secret: str,
        drive_id: str,
        *,
        folder: str = "",
        graph: str = GRAPH,
        token_url: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        if not drive_id or any(c in drive_id for c in "/?# "):
            raise ValidationError("a drive id is required (SharePoint library or OneDrive)")
        self._tenant, self._id, self._secret = tenant, client_id, client_secret
        self._drive, self._folder = drive_id, folder.strip("/")
        self._graph = graph.rstrip("/")
        self._token_url = token_url or TOKEN_URL.format(tenant=tenant)
        self._http = http or httpx.AsyncClient(timeout=60.0)
        self._token: tuple[str, float] | None = None

    async def _access(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        try:
            response = await self._http.post(
                self._token_url,
                data={"grant_type": "client_credentials", "client_id": self._id,
                      "client_secret": self._secret, "scope": SCOPE},
            )  # fmt: skip
            response.raise_for_status()
            body = response.json()
            self._token = (
                str(body["access_token"]),
                time.time() + float(body.get("expires_in", 3600)),
            )
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise UpstreamError("Microsoft Graph refused the app credentials") from exc
        return self._token[0]

    async def _get(self, url: str, **params: str) -> httpx.Response:
        try:
            response = await self._http.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {await self._access()}"},
                follow_redirects=True,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamError("Microsoft Graph request failed") from exc
        return response

    def _children_url(self, item_id: str | None) -> str:
        base = f"{self._graph}/drives/{quote(self._drive, safe='')}"
        if item_id:
            return f"{base}/items/{quote(item_id, safe='')}/children"
        if self._folder:
            return f"{base}/root:/{quote(self._folder)}:/children"
        return f"{base}/root/children"

    async def documents(self) -> AsyncIterator[Document]:
        found = 0
        pending: list[tuple[str | None, int]] = [(None, 0)]
        while pending:
            folder, depth = pending.pop()
            url: str | None = self._children_url(folder)
            params: dict[str, str] = {"$top": "200"}
            while url:
                try:
                    body: dict[str, Any] = (await self._get(url, **params)).json()
                except ValueError as exc:
                    raise UpstreamError("Microsoft Graph returned an invalid listing") from exc
                for entry in body.get("value", []):
                    if "folder" in entry:
                        if depth < MAX_DEPTH:
                            pending.append((str(entry["id"]), depth + 1))
                        continue
                    name = str(entry.get("name", ""))
                    readable = PurePosixPath(name).suffix.lower() in READABLE
                    if "file" not in entry or not readable or int(entry.get("size", 0)) > MAX_BYTES:
                        continue
                    found += 1
                    if found > MAX_FILES:
                        raise UpstreamError(f"more than {MAX_FILES} files in the library")
                    yield Document(
                        str(entry["id"]), name, self._loader(str(entry["id"]), name),
                        str(entry.get("eTag") or entry.get("lastModifiedDateTime") or "") or None,
                        ("source/sharepoint",),
                    )  # fmt: skip
                url, params = body.get("@odata.nextLink"), {}

    def _loader(self, item_id: str, name: str) -> Callable[[], Awaitable[tuple[str, bytes]]]:
        async def load() -> tuple[str, bytes]:
            drive, item = quote(self._drive, safe=""), quote(item_id, safe="")
            url = f"{self._graph}/drives/{drive}/items/{item}/content"
            return name, (await self._get(url)).content

        return load
