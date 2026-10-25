"""Split long documents into items that fit the item size limit, at headings or paragraphs."""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_SECTION_CHARS = 40_000
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Section:
    heading: str | None
    text: str


def _by_headings(text: str) -> list[Section]:
    marks = list(_HEADING.finditer(text))
    if not marks:
        return [Section(None, text)]
    out = [Section(None, text[: marks[0].start()])] if text[: marks[0].start()].strip() else []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append(Section(m.group(2), text[m.start() : end].rstrip()))
    return out


def _hard_split(text: str, limit: int) -> list[str]:
    """Pieces of at most `limit` characters, preferring paragraph, then line, then space breaks."""
    pieces = []
    while len(text) > limit:
        for sep in ("\n\n", "\n", " "):
            found = text.rfind(sep, limit // 2, limit)
            if found > 0:
                cut = found
                break
        else:
            cut = limit
        pieces.append(text[:cut])
        text = text[cut:].lstrip()
    if text.strip():
        pieces.append(text)
    return pieces


def split_sections(text: str, limit: int = MAX_SECTION_CHARS) -> list[Section]:
    """Every character of non-blank input lands in exactly one section of at most `limit`
    characters; small neighbouring sections are merged so a document does not become dozens of
    tiny items."""
    raw: list[Section] = []
    for section in _by_headings(text):
        parts = _hard_split(section.text, limit)
        raw += [Section(section.heading if i == 0 else None, p) for i, p in enumerate(parts)]
    merged: list[Section] = []
    for section in raw:
        if merged and len(merged[-1].text) + len(section.text) + 2 <= limit:
            last = merged[-1]
            merged[-1] = Section(last.heading, f"{last.text}\n\n{section.text}")
        else:
            merged.append(section)
    return [s for s in merged if s.text.strip()]
