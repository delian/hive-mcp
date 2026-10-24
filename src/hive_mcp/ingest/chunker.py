"""Structure-aware chunking: paragraphs are packed into chunks of about `max_words` words.

Every word of the input lands in at least one chunk; over-long paragraphs are split with a
small overlap so a sentence cut at a boundary is still seen whole by one chunk.
"""

from __future__ import annotations

import re

MAX_WORDS = 120
OVERLAP_WORDS = 20
_BLANK_LINES = re.compile(r"\n\s*\n")


def _split_long(words: list[str], max_words: int, overlap: int) -> list[str]:
    step = max(max_words - overlap, 1)
    pieces = []
    for start in range(0, len(words), step):
        pieces.append(" ".join(words[start : start + max_words]))
        if start + max_words >= len(words):
            break
    return pieces


def chunk_text(text: str, max_words: int = MAX_WORDS, overlap: int = OVERLAP_WORDS) -> list[str]:
    """Deterministic; returns [] for text without any word. Never returns an empty chunk."""
    chunks: list[str] = []
    current: list[str] = []
    count = 0
    for paragraph in _BLANK_LINES.split(text):
        words = paragraph.split()
        if not words:
            continue
        if len(words) > max_words:
            if current:
                chunks.append("\n\n".join(current))
                current, count = [], 0
            chunks.extend(_split_long(words, max_words, overlap))
            continue
        if count + len(words) > max_words and current:
            chunks.append("\n\n".join(current))
            current, count = [], 0
        current.append(" ".join(words))
        count += len(words)
    if current:
        chunks.append("\n\n".join(current))
    return chunks
