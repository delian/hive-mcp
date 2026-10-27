"""Connectors for Confluence (pages of a space) and Jira (issues matching a JQL query).

Both use the REST APIs with an Atlassian account email + API token (Cloud) over HTTP basic auth.
The page/issue version or update time is the change marker, so unchanged ones are not downloaded
again. Atlassian's rich text (Jira's document format) is flattened to plain text.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import httpx

from hive_mcp import __version__
from hive_mcp.connectors.base import Document
from hive_mcp.domain.errors import ValidationError
from hive_mcp.security.oauth.upstream import UpstreamError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

PAGE = 50
MAX_ITEMS = 2000
HTTP_TIMEOUT = 30.0


def _client(email: str, token: str, http: httpx.AsyncClient | None) -> httpx.AsyncClient:
    if http is not None:
        return http
    basic = base64.b64encode(f"{email}:{token}".encode()).decode()
    return httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers={"Authorization": f"Basic {basic}", "User-Agent": f"hive-mcp/{__version__}"},
    )


def _check_base(url: str) -> str:
    if not url.startswith("https://"):
        raise ValidationError("the Atlassian base address must start with https://")
    return url.rstrip("/")


async def _get(http: httpx.AsyncClient, url: str, **params: str) -> dict[str, Any]:
    try:
        response = await http.get(url, params=params)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise UpstreamError("Atlassian request failed") from exc
    if not isinstance(body, dict):
        raise UpstreamError("Atlassian returned an unexpected document")
    return body


class ConfluenceConnector:
    def __init__(
        self,
        base_url: str,
        space: str,
        email: str,
        token: str,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        if not space or not space.replace("~", "").replace("_", "").isalnum():
            raise ValidationError("a Confluence space key is required (letters and digits)")
        self._base, self._space = _check_base(base_url), space
        self._http = _client(email, token, http)

    async def documents(self) -> AsyncIterator[Document]:
        start, seen = 0, 0
        while True:
            body = await _get(
                self._http, f"{self._base}/rest/api/content", spaceKey=self._space, type="page",
                status="current", expand="version", limit=str(PAGE), start=str(start),
            )  # fmt: skip
            results = body.get("results", [])
            for page in results:
                seen += 1
                if seen > MAX_ITEMS:
                    raise UpstreamError(f"more than {MAX_ITEMS} pages in the space")
                page_id = str(page["id"])
                version = str((page.get("version") or {}).get("number", ""))
                title = str(page.get("title", page_id))
                yield Document(
                    page_id, title, self._loader(page_id, title), version or None,
                    (f"space/{self._space}".lower(),),
                )  # fmt: skip
            if len(results) < PAGE:
                return
            start += PAGE

    def _loader(self, page_id: str, title: str) -> Callable[[], Awaitable[tuple[str, bytes]]]:
        async def load() -> tuple[str, bytes]:
            body = await _get(
                self._http, f"{self._base}/rest/api/content/{page_id}", expand="body.storage"
            )
            html = str(((body.get("body") or {}).get("storage") or {}).get("value", ""))
            return f"{title}.html", f"<h1>{_escape(title)}</h1>{html}".encode()

        return load


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def adf_text(node: object) -> str:
    """Plain text of an Atlassian document (Jira descriptions and comments)."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return str(node.get("text", ""))
    parts = [adf_text(child) for child in node.get("content", []) or []]
    joiner = "\n" if node.get("type") in {"doc", "bulletList", "orderedList", "codeBlock"} else ""
    text = joiner.join(parts)
    return text + "\n" if node.get("type") in {"paragraph", "heading", "listItem"} else text


class JiraConnector:
    def __init__(
        self,
        base_url: str,
        jql: str,
        email: str,
        token: str,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        if not jql.strip():
            raise ValidationError("a JQL query is required (which issues to import)")
        self._base, self._jql = _check_base(base_url), jql
        self._http = _client(email, token, http)

    async def documents(self) -> AsyncIterator[Document]:
        token, seen = "", 0
        while True:
            params = {
                "jql": self._jql, "maxResults": str(PAGE), "fields": "summary,updated",
            }  # fmt: skip
            if token:
                params["nextPageToken"] = token
            body = await _get(self._http, f"{self._base}/rest/api/3/search/jql", **params)
            for issue in body.get("issues", []):
                seen += 1
                if seen > MAX_ITEMS:
                    raise UpstreamError(f"more than {MAX_ITEMS} issues match the query")
                key = str(issue["key"])
                fields = issue.get("fields") or {}
                yield Document(
                    key, key, self._loader(key), str(fields.get("updated", "")) or None,
                    ("source/jira",),
                )  # fmt: skip
            token = str(body.get("nextPageToken") or "")
            if not token or body.get("isLast"):
                return

    def _loader(self, key: str) -> Callable[[], Awaitable[tuple[str, bytes]]]:
        async def load() -> tuple[str, bytes]:
            body = await _get(
                self._http, f"{self._base}/rest/api/3/issue/{key}",
                fields="summary,description,status,comment,labels",
            )  # fmt: skip
            f = body.get("fields") or {}
            lines = [
                f"# {key}: {f.get('summary', '')}",
                f"Status: {(f.get('status') or {}).get('name', '')}",
            ]
            if f.get("labels"):
                lines.append("Labels: " + ", ".join(map(str, f["labels"])))
            lines += ["", adf_text(f.get("description")).strip()]
            for comment in ((f.get("comment") or {}).get("comments") or [])[:50]:
                who = (comment.get("author") or {}).get("displayName", "someone")
                lines += ["", f"Comment by {who}:", adf_text(comment.get("body")).strip()]
            return f"{key}.md", "\n".join(lines).encode()

        return load
