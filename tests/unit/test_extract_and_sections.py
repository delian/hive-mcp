import io

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from hive_mcp.domain.errors import ValidationError
from hive_mcp.ingest.extract import extract_text
from hive_mcp.ingest.files import files_under
from hive_mcp.ingest.sections import split_sections


def test_text_decoding_variants():
    assert extract_text("a.txt", "héllo\r\nworld\n".encode()).text == "héllo\nworld"
    assert extract_text("a.md", b"\xef\xbb\xbf# Title\nbody").text == "# Title\nbody"  # BOM
    assert extract_text("a.txt", "hi there".encode("utf-16")).text == "hi there"
    assert "caf" in extract_text("a.txt", b"caf\xe9 au lait").text  # legacy latin-1 still readable
    assert extract_text("Makefile", b"all:\n\techo hi").format == "text"  # no extension


def test_html_keeps_headings_and_drops_scripts_and_styles():
    html = (
        b"<html><head><title>Radar notes</title><style>p{}</style></head><body>"
        b"<script>alert(1)</script><h2>Range</h2><p>Pulse &amp; echo</p><ul><li>one</li><li>two</li></ul>"
        b"</body></html>"
    )
    got = extract_text("page.html", html)
    assert got.title == "Radar notes" and got.format == "html"
    assert (
        "## Range" in got.text
        and "Pulse & echo" in got.text
        and "one\ntwo" in got.text.replace("\n\n", "\n")
    )
    assert "alert" not in got.text and "p{}" not in got.text


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("x.bin", b"abc"),
        ("x.png", b"\x89PNG"),
        ("x.txt", b"ab\x00cd"),
        ("x.txt", b"   \n "),
        ("x.pdf", b"%PDF-garbage"),
    ],
)
def test_unreadable_or_unsupported_files_are_refused_with_a_message(name, data):
    with pytest.raises(ValidationError):
        extract_text(name, data)


def make_pdf(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_pdf_text_is_extracted_even_without_the_extension():
    got = extract_text("report", make_pdf("Pulse repetition frequency"))
    assert got.format == "pdf" and "Pulse repetition frequency" in got.text


def test_sections_small_document_is_one_section():
    assert [s.text for s in split_sections("just a paragraph")] == ["just a paragraph"]


def test_sections_split_at_headings_and_merge_small_neighbours():
    text = "intro\n\n# A\n" + "a " * 30 + "\n\n# B\n" + "b " * 30
    assert len(split_sections(text, limit=10_000)) == 1  # all fit one item
    parts = split_sections(text, limit=100)
    assert [p.heading for p in parts][:2] == [None, "A"] or parts[0].heading in (None, "A")
    assert all(len(p.text) <= 100 for p in parts)


@given(
    st.text(alphabet="abc \n#", min_size=1, max_size=3000), st.integers(min_value=20, max_value=400)
)
def test_sections_never_exceed_the_limit_and_never_lose_words(text, limit):
    parts = split_sections(text, limit=limit)
    assert all(len(p.text) <= limit for p in parts)
    assert "".join("".join(p.text.split()) for p in parts) == "".join(text.split())


def test_files_under_skips_hidden_and_symlinks(tmp_path):
    (tmp_path / "a.md").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("y")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("z")
    (tmp_path / "link.txt").symlink_to(tmp_path / "a.md")
    names = [p.name for p in files_under([tmp_path])]
    assert names == ["a.md", "b.txt"]
    assert [p.name for p in files_under([tmp_path / "a.md"])] == ["a.md"]


def test_merging_respects_the_limit_exactly():
    text = "# A\n" + "a" * 46 + "\n\n# B\n" + "b" * 46  # two sections of 50 chars each
    assert len(split_sections(text, limit=102)) == 1  # 50 + 2 + 50 fits
    assert len(split_sections(text, limit=101)) == 2  # one more character would not


def test_pdfs_with_too_many_pages_are_refused(monkeypatch):
    from hive_mcp.ingest import extract  # noqa: PLC0415

    class Reader:
        def __init__(self, _):
            self.is_encrypted, self.metadata, self.pages = False, None, [object()] * 4

    monkeypatch.setattr("pypdf.PdfReader", Reader)
    monkeypatch.setattr(extract, "MAX_PDF_PAGES", 3)
    with pytest.raises(ValidationError, match="more than 3 pages"):
        extract.extract_text("a.pdf", b"%PDF-1.4")
