"""Text from Microsoft Office files (.docx, .xlsx, .pptx), read with the standard library only.

These are zip files of XML. Both layers are untrusted input, so: member sizes are capped before
anything is decompressed, and documents with a DTD or entity declaration are refused (Office XML
never has one; it is how "billion laughs" attacks work on parsers that resolve entities).
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from hive_mcp.domain.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable

MAX_MEMBER_BYTES = 16 * 1024 * 1024  # an XML tree takes ~10x its text in memory
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 5000
MAX_ROWS = 20_000
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_NUMBERED = re.compile(r"(\d+)\.xml$")
_HEADING = re.compile(r"^(?:Heading|heading)\s*(\d)$")


def _open(data: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValidationError("not a valid Office file (damaged or not a zip)") from exc
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
        raise ValidationError("Office file is too large when unpacked")
    if any(i.file_size > MAX_MEMBER_BYTES for i in infos):
        raise ValidationError("Office file contains an oversized part")
    return archive


def _xml(archive: zipfile.ZipFile, name: str) -> ET.Element | None:
    try:
        raw = archive.read(name)
    except KeyError:
        return None
    if b"<!DOCTYPE" in raw[:4096].upper() or b"<!ENTITY" in raw.upper():
        raise ValidationError("Office file contains a forbidden XML declaration")
    try:
        return ET.fromstring(raw)  # noqa: S314 - DTDs were refused above
    except ET.ParseError as exc:
        raise ValidationError(f"damaged Office XML in {name}") from exc


def _text(node: ET.Element, text_tag: str, break_tags: tuple[str, ...] = ()) -> str:
    parts = []
    for el in node.iter():
        if el.tag == text_tag and el.text:
            parts.append(el.text)
        elif el.tag in break_tags:
            parts.append("\n" if el.tag.endswith("}br") else "\t")
    return "".join(parts)


def docx_text(data: bytes) -> str:
    root = _xml(_open(data), "word/document.xml")
    if root is None:
        raise ValidationError("not a Word document (no word/document.xml)")
    lines: list[str] = []

    def paragraph(p: ET.Element) -> str:
        style = p.find(f"{W}pPr/{W}pStyle")
        text = _text(p, f"{W}t", (f"{W}br", f"{W}tab")).strip()
        level = _HEADING.match(style.get(f"{W}val", "")) if style is not None and text else None
        return f"{'#' * min(int(level.group(1)), 6)} {text}" if level else text

    body = root.find(f"{W}body")
    for child in body if body is not None else []:
        if child.tag == f"{W}p":
            lines.append(paragraph(child))
        elif child.tag == f"{W}tbl":
            for row in child.iter(f"{W}tr"):
                cells = [
                    " ".join(paragraph(p) for p in c.iter(f"{W}p")).strip()
                    for c in row.iter(f"{W}tc")
                ]
                lines.append(" | ".join(cells))
            lines.append("")
    return "\n".join(lines)


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    root = _xml(archive, "xl/sharedStrings.xml")
    return [] if root is None else [_text(si, f"{S}t") for si in root.iter(f"{S}si")]


def _sheets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """(name, part path) of each worksheet in workbook order, following the workbook's own
    relationships; falls back to numeric file order when they are missing."""
    book = _xml(archive, "xl/workbook.xml")
    rels = _xml(archive, "xl/_rels/workbook.xml.rels")
    targets = {} if rels is None else {r.get("Id", ""): r.get("Target", "") for r in rels}
    found = []
    for sheet in [] if book is None else book.iter(f"{S}sheet"):
        target = targets.get(sheet.get(f"{R}id", ""), "")
        path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
        if target and path in archive.namelist():
            found.append((sheet.get("name", ""), path))
    if found:
        return found
    parts = sorted(
        (
            n
            for n in archive.namelist()
            if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
        ),
        key=lambda n: int(m.group(1)) if (m := _NUMBERED.search(n)) else 0,
    )
    return [(f"Sheet {i}", p) for i, p in enumerate(parts, start=1)]


def _column(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref)
    index = 0
    for ch in letters.group(0) if letters else "A":
        index = index * 26 + ord(ch) - 64
    return index - 1


def xlsx_text(data: bytes) -> str:
    archive = _open(data)
    strings, sheets = _shared_strings(archive), _sheets(archive)
    if not sheets:
        raise ValidationError("not an Excel workbook (no worksheets)")
    out: list[str] = []
    for name, part in sheets:
        root = _xml(archive, part)
        if root is None:
            continue
        out.append(f"## {name}")
        for count, row in enumerate(root.iter(f"{S}row")):
            if count >= MAX_ROWS:
                out.append(f"[... more than {MAX_ROWS} rows omitted]")
                break
            cells: dict[int, str] = {}
            for c in row.iter(f"{S}c"):
                kind, value = c.get("t"), c.find(f"{S}v")
                if kind == "inlineStr":
                    text = _text(c, f"{S}t")
                elif value is None or value.text is None:
                    continue
                elif kind == "s":
                    index = int(value.text)
                    text = strings[index] if 0 <= index < len(strings) else ""
                else:
                    text = value.text
                cells[_column(c.get("r", "A"))] = text.replace("\n", " ").strip()
            if cells:
                out.append(" | ".join(cells.get(i, "") for i in range(max(cells) + 1)))
        out.append("")
    return "\n".join(out)


def pptx_text(data: bytes) -> str:
    archive = _open(data)
    slides = sorted(
        (n for n in archive.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
        key=lambda n: int(m.group(1)) if (m := _NUMBERED.search(n)) else 0,
    )
    if not slides:
        raise ValidationError("not a PowerPoint file (no slides)")
    out: list[str] = []
    for number, name in enumerate(slides, start=1):
        root = _xml(archive, name)
        if root is None:
            continue
        out.append(f"## Slide {number}")
        for shape in root.iter(f"{P}sp"):
            title = shape.find(f"{P}nvSpPr/{P}nvPr/{P}ph")
            is_title = title is not None and title.get("type") in {"title", "ctrTitle"}
            for para in shape.iter(f"{A}p"):
                text = _text(para, f"{A}t", (f"{A}br",)).strip()
                if text:
                    out.append(f"### {text}" if is_title else text)
        notes = _xml(archive, name.replace("slides/slide", "notesSlides/notesSlide"))
        if notes is not None:
            spoken = " ".join(
                t.strip() for t in (_text(p, f"{A}t") for p in notes.iter(f"{A}p")) if t.strip()
            )
            if spoken:
                out.append(f"Notes: {spoken}")
        out.append("")
    return "\n".join(out)


OFFICE: dict[str, tuple[str, Callable[[bytes], str]]] = {
    ".docx": ("docx", docx_text),
    ".xlsx": ("xlsx", xlsx_text),
    ".pptx": ("pptx", pptx_text),
}
LEGACY = frozenset({".doc", ".xls", ".ppt"})
