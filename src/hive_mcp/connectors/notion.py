"""Connector for Notion pages (all pages the integration was shared with, or one database).

The integration only sees pages that someone explicitly shared with it, which is the access
control. `last_edited_time` is the change marker. Blocks are flattened to text, three levels deep.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from hive_mcp import __version__
from hive_mcp.connectors.base import Document
from hive_mcp.security.oauth.upstream import UpstreamError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MAX_PAGES = 2000
MAX_DEPTH = 3
MAX_BLOCKS = 3000
PREFIXES = {
    "heading_1": "# ", "heading_2": "## ", "heading_3": "### ", "bulleted_list_item": "- ",
    "numbered_list_item": "1. ", "to_do": "- [ ] ", "quote": "> ", "code": "    ",
}  # fmt: skip


def rich_text(parts: object) -> str:
    return (
        "".join(str(p.get("plain_text", "")) for p in parts if isinstance(p, dict))
        if isinstance(parts, list)
        else ""
    )


def page_title(page: dict[str, Any]) -> str:
    for prop in (page.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return rich_text(prop.get("title")) or "Untitled"
    return "Untitled"


class NotionConnector:
    def __init__(
        self,
        token: str,
        *,
        database: str | None = None,
        api_base: str = API,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._database, self._api = database, api_base.rstrip("/")
        self._http = http or httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION,
                "User-Agent": f"hive-mcp/{__version__}",
            },
        )  # fmt: skip

    async def _call(self, method: str, path: str, **kw: Any) -> dict[str, Any]:  # noqa: ANN401
        try:
            response = await self._http.request(method, f"{self._api}{path}", **kw)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamError("Notion request failed") from exc
        return body if isinstance(body, dict) else {}

    async def documents(self) -> AsyncIterator[Document]:
        cursor, seen = None, 0
        while True:
            payload: dict[str, Any] = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            if self._database:
                body = await self._call("POST", f"/databases/{self._database}/query", json=payload)
            else:
                payload["filter"] = {"property": "object", "value": "page"}
                body = await self._call("POST", "/search", json=payload)
            for page in body.get("results", []):
                if page.get("object") != "page" or page.get("archived"):
                    continue
                seen += 1
                if seen > MAX_PAGES:
                    raise UpstreamError(f"more than {MAX_PAGES} pages")
                title = page_title(page)
                yield Document(
                    str(page["id"]), title, self._loader(str(page["id"]), title),
                    str(page.get("last_edited_time", "")) or None, ("source/notion",),
                )  # fmt: skip
            cursor = body.get("next_cursor")
            if not body.get("has_more") or not cursor:
                return

    def _loader(self, page_id: str, title: str) -> Callable[[], Awaitable[tuple[str, bytes]]]:
        async def load() -> tuple[str, bytes]:
            lines: list[str] = [f"# {title}"]
            budget = [MAX_BLOCKS]
            await self._blocks(page_id, 0, lines, budget)
            return f"{title}.md", "\n".join(lines).encode()

        return load

    async def _blocks(self, block_id: str, depth: int, lines: list[str], budget: list[int]) -> None:
        cursor = None
        while budget[0] > 0:
            params = {"page_size": "100", **({"start_cursor": cursor} if cursor else {})}
            body = await self._call("GET", f"/blocks/{block_id}/children", params=params)
            for block in body.get("results", []):
                budget[0] -= 1
                kind = str(block.get("type", ""))
                body = block.get(kind) or {}
                text = rich_text(body.get("rich_text"))
                prefix = PREFIXES.get(kind, "")
                if kind == "table_row":  # a table's text lives in `cells`, not in `rich_text`
                    cells = body.get("cells")
                    text = (
                        " | ".join(rich_text(c) for c in cells) if isinstance(cells, list) else ""
                    )
                elif kind == "to_do" and body.get("checked"):
                    prefix = "- [x] "
                if text:
                    lines.append(f"{'  ' * depth}{prefix}{text}")
                if block.get("has_children") and depth < MAX_DEPTH:
                    await self._blocks(str(block["id"]), depth + 1, lines, budget)
            cursor = body.get("next_cursor")
            if not body.get("has_more") or not cursor:
                return
