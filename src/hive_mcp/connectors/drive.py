"""Connector for a Google Drive folder (Docs, Sheets, Slides, PDFs, Office files, text).

A service account with domain-wide delegation reads as one Workspace user, with the read-only
Drive scope. Google Docs and Slides are exported as text, Sheets as CSV; other files are downloaded
when their type is one we can read. The modified time (or checksum) is the change marker.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import httpx

from hive_mcp.connectors.base import Document
from hive_mcp.ingest.git import READABLE
from hive_mcp.security.oauth.upstream import UpstreamError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from hive_mcp.security.google_auth import ServiceAccountToken

SCOPE = "https://www.googleapis.com/auth/drive.readonly"
API = "https://www.googleapis.com/drive/v3"
FOLDER = "application/vnd.google-apps.folder"
EXPORTS = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
}
MAX_DEPTH = 5
MAX_FILES = 2000
MAX_BYTES = 20 * 1024 * 1024
PAGE = 200


class DriveConnector:
    def __init__(
        self,
        folder_id: str,
        auth: ServiceAccountToken,
        *,
        api_base: str = API,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        if not folder_id or any(c in folder_id for c in "'\\ /"):
            raise UpstreamError("a Drive folder id is required (the last part of the folder URL)")
        self._folder, self._auth, self._api = folder_id, auth, api_base.rstrip("/")
        self._http = http or auth.http

    async def _get(self, url: str, **params: str) -> httpx.Response:
        try:
            response = await self._http.get(
                url, params=params, headers={"Authorization": f"Bearer {await self._auth.get()}"}
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamError("Google Drive request failed") from exc
        return response

    async def _children(self, folder: str) -> AsyncIterator[dict[str, Any]]:
        token = ""
        while True:
            params = {
                "q": f"'{folder}' in parents and trashed = false",
                "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime,md5Checksum)",
                "pageSize": str(PAGE),
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if token:
                params["pageToken"] = token
            try:
                body = (await self._get(f"{self._api}/files", **params)).json()
            except ValueError as exc:
                raise UpstreamError("Google Drive returned an invalid listing") from exc
            for entry in body.get("files", []):
                yield entry
            token = body.get("nextPageToken", "")
            if not token:
                return

    async def documents(self) -> AsyncIterator[Document]:
        found = 0
        pending = [(self._folder, 0)]
        while pending:
            folder, depth = pending.pop()
            async for entry in self._children(folder):
                if entry.get("mimeType") == FOLDER:
                    if depth < MAX_DEPTH:
                        pending.append((str(entry["id"]), depth + 1))
                    continue
                doc = self._document(entry)
                if doc is None:
                    continue
                found += 1
                if found > MAX_FILES:
                    raise UpstreamError(f"more than {MAX_FILES} files in the folder tree")
                yield doc

    def _document(self, entry: dict[str, Any]) -> Document | None:
        mime, name, file_id = str(entry.get("mimeType", "")), str(entry["name"]), str(entry["id"])
        version = str(entry.get("md5Checksum") or entry.get("modifiedTime") or "") or None
        if mime in EXPORTS:
            export, suffix = EXPORTS[mime]
            return Document(file_id, name, self._exporter(file_id, name + suffix, export), version)
        if (
            PurePosixPath(name).suffix.lower() in READABLE
            and int(entry.get("size", 0)) <= MAX_BYTES
        ):
            return Document(file_id, name, self._downloader(file_id, name), version)
        return None  # videos, images, shortcuts...: nothing to read

    def _exporter(
        self, file_id: str, name: str, mime: str
    ) -> Callable[[], Awaitable[tuple[str, bytes]]]:
        async def load() -> tuple[str, bytes]:
            response = await self._get(f"{self._api}/files/{file_id}/export", mimeType=mime)
            return name, response.content

        return load

    def _downloader(self, file_id: str, name: str) -> Callable[[], Awaitable[tuple[str, bytes]]]:
        async def load() -> tuple[str, bytes]:
            response = await self._get(
                f"{self._api}/files/{file_id}", alt="media", supportsAllDrives="true"
            )
            return name, response.content

        return load
