"""Connector for the readable files of a GitHub repository (docs, READMEs, runbooks).

Uses the REST API (a tree listing, then one raw download per changed file), so nothing is cloned
and no `git` binary is needed. The blob sha is the change marker: unchanged files are not
downloaded again. GitHub Enterprise works through `api_base`.
"""

from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from hive_mcp import __version__
from hive_mcp.connectors.base import Document
from hive_mcp.domain.errors import ValidationError
from hive_mcp.ingest.git import DEFAULT_EXCLUDES, MAX_FILE_BYTES, READABLE
from hive_mcp.security.oauth.upstream import UpstreamError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

API = "https://api.github.com"
HTTP_TIMEOUT = 30.0
MAX_FILES = 2000


class GithubConnector:
    def __init__(  # noqa: PLR0913 - keyword-only options of one repository
        self,
        repo: str,
        *,
        ref: str = "HEAD",
        paths: tuple[str, ...] = (),
        token: str | None = None,
        api_base: str = API,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        owner, _, name = repo.partition("/")
        if not owner or not name or "/" in name or any(c in repo for c in " \\?#"):
            raise ValidationError(f"repository must look like owner/name, not {repo!r}")
        self._repo, self._ref, self._paths = repo, ref, tuple(p.strip("/") for p in paths)
        self._api = api_base.rstrip("/")
        headers = {"User-Agent": f"hive-mcp/{__version__}", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = http or httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers)

    def _wanted(self, entry: dict[str, Any]) -> bool:
        path = str(entry.get("path", ""))
        pure = PurePosixPath(path)
        if entry.get("type") != "blob" or int(entry.get("size", 0)) > MAX_FILE_BYTES:
            return False
        if self._paths and not any(path == p or path.startswith(p + "/") for p in self._paths):
            return False
        if pure.suffix.lower() not in READABLE and pure.name not in {"Makefile", "Dockerfile"}:
            return False
        return not any(
            fnmatch.fnmatch(path, x) or fnmatch.fnmatch(pure.name, x) for x in DEFAULT_EXCLUDES
        )

    async def documents(self) -> AsyncIterator[Document]:
        try:
            response = await self._http.get(
                f"{self._api}/repos/{self._repo}/git/trees/{self._ref}", params={"recursive": "1"}
            )
            response.raise_for_status()
            tree = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamError(f"could not list {self._repo} on GitHub") from exc
        if tree.get("truncated"):
            raise UpstreamError(f"{self._repo} is too large to list; narrow it with `paths`")
        entries = [e for e in tree.get("tree", []) if self._wanted(e)]
        if len(entries) > MAX_FILES:
            raise UpstreamError(f"{len(entries)} files in {self._repo}; narrow it with `paths`")
        for entry in entries:
            path = str(entry["path"])
            yield Document(
                path,
                path,
                self._loader(path),
                str(entry.get("sha") or ""),
                (f"repo/{self._repo}".lower(),),
            )

    def _loader(self, path: str) -> Callable[[], Awaitable[tuple[str, bytes]]]:
        async def load() -> tuple[str, bytes]:
            try:
                response = await self._http.get(
                    f"{self._api}/repos/{self._repo}/contents/{quote(path)}",
                    params={"ref": self._ref},
                    headers={"Accept": "application/vnd.github.raw+json"},
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise UpstreamError(f"could not download {path}") from exc
            return PurePosixPath(path).name, response.content

        return load
