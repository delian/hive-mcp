"""File bytes -> plain text: text, HTML and Office are built in; PDF needs the `documents` extra."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath

from hive_mcp.domain.errors import ValidationError
from hive_mcp.ingest.office import LEGACY, OFFICE

TEXT_SUFFIXES = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".csv",
        ".tsv",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".log",
        ".py",
        ".js",
        ".ts",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".h",
        ".cpp",
        ".sh",
        ".sql",
        ".xml",
        ".tex",
        "",
    }
)
HTML_SUFFIXES = frozenset({".html", ".htm"})
SKIPPED_TAGS = frozenset({"script", "style", "head", "noscript", "template"})
BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre"}
)
_BLANKS = re.compile(r"\n{3,}")


@dataclass(frozen=True, slots=True)
class Extracted:
    text: str
    format: str  # "text", "html" or "pdf" (used as a tag)
    title: str | None = None


class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag in SKIPPED_TAGS:
            self._skip += 1
        if tag in BLOCK_TAGS:
            self.parts.append("\n" if tag in {"br", "li", "tr"} else "\n\n")
            if tag.startswith("h") and len(tag) == 2:  # noqa: PLR2004 - h1..h6
                self.parts.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in SKIPPED_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def _decode(data: bytes) -> str:
    if data[:2] in {b"\xff\xfe", b"\xfe\xff"}:  # UTF-16 with BOM (contains NULs by design)
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise ValidationError("unreadable UTF-16 text") from exc
    if b"\x00" in data[:8192]:
        raise ValidationError("binary file; supported types are text, markdown, html and pdf")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")  # never fails; better than refusing legacy text files


MAX_PDF_PAGES = 2000


def _pdf(data: bytes) -> Extracted:
    try:
        from pypdf import PdfReader  # noqa: PLC0415 - optional extra
    except ImportError as exc:
        raise ValidationError("PDF support needs the 'documents' extra (pypdf)") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValidationError("encrypted PDFs are not supported")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValidationError(f"PDF has more than {MAX_PDF_PAGES} pages; split it first")
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        title = (reader.metadata.title if reader.metadata else None) or None
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(f"cannot read PDF: {exc.__class__.__name__}") from exc
    return Extracted("\n\n".join(p for p in pages if p), "pdf", title)


def extract_text(name: str, data: bytes) -> Extracted:
    suffix = PurePosixPath(name.lower()).suffix
    if data[:5] == b"%PDF-" or suffix == ".pdf":
        result = _pdf(data)
    elif suffix in HTML_SUFFIXES:
        parser = _HtmlText()
        parser.feed(_decode(data))
        parser.close()
        text = _BLANKS.sub("\n\n", "".join(parser.parts)).strip()
        result = Extracted(text, "html", parser.title.strip() or None)
    elif suffix in OFFICE:
        fmt, reader = OFFICE[suffix]
        result = Extracted(_BLANKS.sub("\n\n", reader(data)).strip(), fmt)
    elif suffix in LEGACY:
        raise ValidationError(
            f"old binary Office files ({suffix}) are not supported; save as {suffix}x"
        )
    elif suffix in TEXT_SUFFIXES:
        result = Extracted(_decode(data).replace("\r\n", "\n").strip(), "text")
    else:
        raise ValidationError(f"unsupported file type {suffix!r}")
    if not result.text.strip():
        raise ValidationError("the file contains no readable text")
    return result
