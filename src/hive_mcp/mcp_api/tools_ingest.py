"""Tool for adding documents (text, markdown, html, pdf) to the knowledge base."""

from __future__ import annotations

import base64
import binascii
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.mcp_api.errors import guarded
from hive_mcp.mcp_api.tools import WRITE, current_principal

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server import MCPServer

    from hive_mcp.bootstrap import Services

MAX_TOOL_BYTES = 10 * 1024 * 1024  # larger files: use the `hive ingest` command


class IngestOut(BaseModel):
    ids: list[str] = Field(description="One item per part; part 1 is the head the others link to.")
    parts: int
    new_parts: int = Field(description="Parts stored now; 0 means this file was already ingested.")
    redactions: dict[str, int]


def register_ingest_tools(mcp: MCPServer, get_services: Callable[[], Services]) -> None:
    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_ingest(
        name: str,
        content: str,
        encoding: Literal["text", "base64"] = "text",
        tags: list[str] | None = None,
        share_with_project: str | None = None,
    ) -> IngestOut:
        """Add a document (.txt .md .html .pdf, source code...) as searchable, linked knowledge.

        `name` is the file name (its extension picks the reader). Send binary files (pdf) as base64.
        Long documents become several parts linked with part_of/next. Private unless
        `share_with_project`. Re-sending the same file does nothing. Up to 10 MB; use `hive ingest`
        for more. Secrets found in the text are masked.
        """
        try:
            data = (
                base64.b64decode(content, validate=True)
                if encoding == "base64"
                else content.encode()
            )
        except (binascii.Error, ValueError) as exc:
            raise ValidationError("content is not valid base64") from exc
        if len(data) > MAX_TOOL_BYTES:
            raise ValidationError("file too large for this tool; use `hive ingest`")
        scope = Scope(ScopeLevel.PROJECT, share_with_project) if share_with_project else None
        result = await get_services().ingest.ingest(
            current_principal(), name, data, tags=tuple(tags or ()), scope=scope
        )
        return IngestOut(
            ids=[i.id for i in result.items],
            parts=len(result.items),
            new_parts=result.created,
            redactions=result.redactions,
        )
