"""Pick the chunk of an item to show for a query."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.ingest.chunker import chunk_text
from hive_mcp.search.tokenize import tokenize

if TYPE_CHECKING:
    from collections.abc import Sequence


def best_chunk_index(chunks: Sequence[str], query: str, preferred: int | None = None) -> int:
    """Chunk with the most distinct query words; ties go to `preferred`, then the first chunk."""
    terms = set(tokenize(query))
    if not chunks:
        return 0
    start = preferred if preferred is not None and 0 <= preferred < len(chunks) else 0
    best, best_hits = start, len(terms & set(tokenize(chunks[start])))
    for index, chunk in enumerate(chunks):
        hits = len(terms & set(tokenize(chunk)))
        if hits > best_hits:
            best, best_hits = index, hits
    return best


def snippet_for(body: str, query: str, preferred: int | None = None, limit: int = 400) -> str:
    chunks = chunk_text(body) or [body]
    return chunks[best_chunk_index(chunks, query, preferred)][:limit]
