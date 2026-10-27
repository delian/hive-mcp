"""File bytes -> plain text: text, HTML and Office are built in; PDF needs the `documents` extra."""

from __future__ import annotations

import codecs
import io
import re
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import PurePosixPath

from hive_mcp.domain.errors import ValidationError
from hive_mcp.ingest.ocr import IMAGE_SUFFIXES, OcrConfig, ocr_image, ocr_pdf_pages
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
# `head` is not skipped: its end tag may be omitted, which would swallow the whole page (the
# title is read separately and its other children are metadata without text).
SKIPPED_TAGS = frozenset({"script", "style", "noscript", "template"})
BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "pre",
        "blockquote",
        "table",
        "ul",
        "ol",
        "dt",
        "dd",
        "hr",
        "caption",
    }
)
_BLANKS = re.compile(r"\n{3,}")
_META_CHARSET = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]+)", re.IGNORECASE)
_XML_ENCODING = re.compile(rb"^\s*<\?xml[^>]*encoding\s*=\s*[\"']([A-Za-z0-9_.:-]+)", re.IGNORECASE)


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
        self._cells = 0  # cells already written in the current table row

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag == "tr":
            self._cells = 0
        elif tag in {"td", "th"}:  # cells of a row must not run together ("NameAge")
            self.parts.append(" | " if self._cells else "")
            self._cells += 1
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


def _declared(data: bytes, pattern: re.Pattern[bytes] | None) -> str | None:
    """A single-byte or CJK encoding the document names for itself (<meta charset>, <?xml ...?>)."""
    found = pattern.search(data[:4096]) if pattern else None
    if found is None:
        return None
    try:
        name = codecs.lookup(found.group(1).decode("ascii")).name
    except LookupError:
        return None
    return None if name.startswith(("utf-16", "utf-32", "utf-8")) else name


def _decode(data: bytes, declared: re.Pattern[bytes] | None = None) -> str:
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
        pass
    if (name := _declared(data, declared)) is not None:  # windows-1251 pages, Shift_JIS, ...
        return data.decode(name, errors="replace")
    # A UTF-8 document with a stray bad byte (a cut multi-byte character, one pasted glitch) must
    # not turn every accent or Cyrillic letter into mojibake: when valid multi-byte text clearly
    # dominates, keep UTF-8 and replace only the bad bytes.
    lenient = data.decode("utf-8", errors="replace")
    bad = lenient.count("\ufffd")
    good = sum(1 for ch in lenient if not ch.isascii() and ch != "\ufffd")
    if good >= 8 * bad:
        return lenient.removeprefix("\ufeff")
    return data.decode("latin-1")  # never fails; better than refusing legacy text files


MAX_PDF_PAGES = 2000
MIN_CHARS_PER_PAGE = 30  # below this a PDF page counts as a scan


def _pdf(data: bytes, ocr: OcrConfig | None) -> Extracted:
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
    fmt = "pdf"
    if ocr is not None:
        pages, scanned = _read_scans(data, pages, ocr)
        fmt = "pdf-ocr" if scanned else "pdf"
    return Extracted("\n\n".join(p for p in pages if p), fmt, title)


def _read_scans(data: bytes, pages: list[str], ocr: OcrConfig) -> tuple[list[str], bool]:
    """Pages with (almost) no text layer are scans: read them with OCR. A mostly-text document with
    a few more blank pages than the OCR limit is left alone rather than rejected."""
    sparse = [i + 1 for i, text in enumerate(pages) if len(text) < MIN_CHARS_PER_PAGE]
    if not sparse or (len(sparse) > ocr.max_pages and len(sparse) * 2 < len(pages)):
        return pages, False
    read = ocr_pdf_pages(data, sparse, ocr)
    return [read.get(i + 1, text) for i, text in enumerate(pages)], True


def extract_text(name: str, data: bytes, ocr: OcrConfig | None = None) -> Extracted:
    suffix = PurePosixPath(name.lower()).suffix
    if data[:5] == b"%PDF-" or suffix == ".pdf":
        result = _pdf(data, ocr)
    elif suffix in IMAGE_SUFFIXES:
        if ocr is None:
            raise ValidationError(
                "images can only be read with OCR (HIVE_OCR=true, see docs/INGEST.md)"
            )
        result = Extracted(ocr_image(data, ocr), "image-ocr")
    elif suffix in HTML_SUFFIXES:
        parser = _HtmlText()
        parser.feed(_decode(data, _META_CHARSET))
        parser.close()
        text = _BLANKS.sub("\n\n", "".join(parser.parts)).strip()
        result = Extracted(text, "html", parser.title.strip() or None)
    elif suffix in OFFICE:
        fmt, reader = OFFICE[suffix]
        try:
            text = reader(data)
        except ValidationError:
            raise
        except Exception as exc:  # damaged or hostile files: a refusal, never a crash
            raise ValidationError(f"cannot read {fmt} file: {exc.__class__.__name__}") from exc
        result = Extracted(_BLANKS.sub("\n\n", text).strip(), fmt)
    elif suffix in LEGACY:
        raise ValidationError(
            f"old binary Office files ({suffix}) are not supported; save as {suffix}x"
        )
    elif suffix in TEXT_SUFFIXES:
        text = _decode(data, _XML_ENCODING).replace("\r\n", "\n").replace("\r", "\n")
        result = Extracted(text.strip(), "text")
    else:
        raise ValidationError(f"unsupported file type {suffix!r}")
    if "\x00" in result.text:  # PDF text layers and odd text files carry stray NULs
        result = replace(result, text=result.text.replace("\x00", ""))
    if not result.text.strip():
        hint = (
            " (a scan? enable OCR: HIVE_OCR=true)" if result.format == "pdf" and ocr is None else ""
        )
        raise ValidationError(f"the file contains no readable text{hint}")
    return result
