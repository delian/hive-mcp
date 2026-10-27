"""OCR of images and scanned PDFs with the real Tesseract/Poppler (skipped where they are absent)."""

from __future__ import annotations

import io
import shutil

import pytest
from PIL import Image, ImageDraw, ImageFont

from hive_mcp.domain.errors import ValidationError
from hive_mcp.ingest import ocr
from hive_mcp.ingest.extract import extract_text
from hive_mcp.ingest.ocr import OcrConfig, check_available

needs_ocr = pytest.mark.skipif(
    not (shutil.which("tesseract") and shutil.which("pdftoppm")), reason="tesseract/poppler absent"
)
CFG = OcrConfig()


def page_image(text: str, size: tuple[int, int] = (1100, 300)) -> Image.Image:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=44)
    for i, line in enumerate(text.split("\n")):
        draw.text((40, 30 + i * 70), line, fill="black", font=font)
    return image


def png(text: str) -> bytes:
    buffer = io.BytesIO()
    page_image(text).save(buffer, "PNG")
    return buffer.getvalue()


def scanned_pdf(*pages: str) -> bytes:
    images = [page_image(t) for t in pages]
    buffer = io.BytesIO()
    images[0].save(buffer, "PDF", save_all=True, append_images=images[1:], resolution=150)
    return buffer.getvalue()


# ---------------------------------------------------------------- configuration
def test_languages_are_validated_because_they_become_a_command_argument():
    assert OcrConfig("eng+deu").languages == "eng+deu"
    for bad in ("", "eng;rm", "eng deu", "-l", "../x", "ENG", "eng+", "+eng", "a" * 40):
        with pytest.raises(ValidationError):
            OcrConfig(bad)
    for kwargs in ({"max_pages": 0}, {"deadline": 0}):
        with pytest.raises(ValidationError):
            OcrConfig(**kwargs)


def test_missing_programs_are_reported_with_the_fix(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(ValidationError, match=r"tesseract, pdftoppm is not installed.*WITH_OCR"):
        check_available()
    with pytest.raises(ValidationError, match=r"^HIVE_OCR is on but tesseract is not"):
        check_available(need_pdf=False)


# ---------------------------------------------------------------- reading
@needs_ocr
def test_an_image_is_read():
    out = extract_text("scan.png", png("Radar range equation\nPulse delay 12 ms"), CFG)
    assert out.format == "image-ocr"
    assert "Radar" in out.text and "range" in out.text and "12" in out.text


@needs_ocr
def test_a_scanned_pdf_is_read_page_by_page():
    out = extract_text(
        "scan.pdf", scanned_pdf("First page about dwell time", "Second page about clutter"), CFG
    )
    assert out.format == "pdf-ocr"
    first, _, second = out.text.partition("\n\n")
    assert "dwell" in first and "clutter" in second  # in page order


@needs_ocr
def test_pdfs_with_a_text_layer_are_not_ocred(monkeypatch):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(612, 792)
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 72 700 Td (A real text layer with enough characters to count) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
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
    buffer = io.BytesIO()
    writer.write(buffer)
    monkeypatch.setattr(ocr, "_run", lambda *a, **k: pytest.fail("OCR must not run for text PDFs"))
    out = extract_text("t.pdf", buffer.getvalue(), CFG)
    assert out.format == "pdf" and "real text layer" in out.text


def test_without_ocr_images_and_scans_say_how_to_enable_it():
    with pytest.raises(ValidationError, match="HIVE_OCR=true"):
        extract_text("scan.png", png("hello world"))
    if shutil.which("pdftoppm"):
        with pytest.raises(ValidationError, match="enable OCR"):
            extract_text("scan.pdf", scanned_pdf("some scanned words here"))


@needs_ocr
def test_garbage_and_blank_input_fail_politely():
    with pytest.raises(ValidationError, match="could not read"):
        extract_text("bad.png", b"this is not an image", CFG)
    blank = io.BytesIO()
    Image.new("RGB", (400, 200), "white").save(blank, "PNG")
    with pytest.raises(ValidationError, match="no readable text"):
        extract_text("blank.png", blank.getvalue(), CFG)
    with pytest.raises(ValidationError):
        extract_text("bad.pdf", b"%PDF-1.4 truncated", CFG)


@needs_ocr
def test_too_many_scanned_pages_are_refused_not_silently_cut():
    pdf = scanned_pdf(*[f"page number {i}" for i in range(4)])
    with pytest.raises(ValidationError, match="over the limit of 3"):
        extract_text("big.pdf", pdf, OcrConfig(max_pages=3))
    assert extract_text("ok.pdf", pdf, OcrConfig(max_pages=4)).format == "pdf-ocr"


@needs_ocr
def test_the_total_deadline_stops_a_long_document():
    ticks = iter([0.0, 0.0, 400.0, 400.0, 400.0])
    pdf = scanned_pdf("one", "two")
    with pytest.raises(ValidationError, match="took longer than 300"):
        ocr.ocr_pdf_pages(pdf, [1, 2], OcrConfig(), clock=lambda: next(ticks))


def test_a_slow_program_is_killed_by_its_time_limit():
    with pytest.raises(ValidationError, match="took longer than"):
        ocr._run(["sleep", "5"], None, 0.3)
    with pytest.raises(ValidationError, match="cannot run"):
        ocr._run(["/no/such/program"], None, 1)


def test_memory_is_limited_for_the_programs(monkeypatch):
    monkeypatch.setattr(ocr, "MEMORY_LIMIT", 64 * 1024 * 1024)
    with pytest.raises(ValidationError):
        ocr._run(["python3", "-c", "x = bytearray(512 * 1024 * 1024); print(len(x))"], None, 20)


@needs_ocr
def test_a_mostly_text_document_with_a_few_extra_blank_pages_is_not_rejected(monkeypatch):
    monkeypatch.setattr(ocr, "ocr_pdf_pages", lambda *a, **k: pytest.fail("should skip OCR"))
    pages = ["x" * 400] * 8 + ["", ""]
    text, scanned = __import__("hive_mcp.ingest.extract", fromlist=["x"])._read_scans(
        b"", pages, OcrConfig(max_pages=1)
    )
    assert scanned is False and text == pages


def test_each_call_gets_only_the_time_that_is_left(monkeypatch):
    seen = []

    def fake_run(cmd, data, timeout):
        seen.append(timeout)
        if cmd[0] == "pdftoppm":
            (__import__("pathlib").Path(cmd[-1]).parent / "page.png").write_bytes(b"png")
        return b"text" if cmd[0] == "tesseract" else b""

    monkeypatch.setattr(ocr, "_run", fake_run)
    ticks = iter([0.0, 290.0, 290.0])  # 10 s of the 300 s are left when the first page starts
    ocr.ocr_pdf_pages(b"%PDF", [1], OcrConfig(), clock=lambda: next(ticks))
    assert seen and all(t <= 10.0 for t in seen)


async def test_the_server_refuses_to_start_with_ocr_on_but_the_programs_missing(
    tmp_path, monkeypatch
):
    from hive_mcp.bootstrap import build_services
    from hive_mcp.config import Settings

    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(ValidationError, match="HIVE_OCR is on"):
        await build_services(Settings(data_dir=tmp_path, ocr=True))


@needs_ocr
async def test_the_configured_languages_and_limits_reach_the_ingest_service(tmp_path):
    from hive_mcp.bootstrap import build_services
    from hive_mcp.config import Settings

    services = await build_services(
        Settings(data_dir=tmp_path, ocr=True, ocr_languages="eng+osd", ocr_max_pages=7)
    )
    try:
        assert services.ingest._ocr == OcrConfig("eng+osd", 7)
    finally:
        await services.close()
    plain = await build_services(Settings(data_dir=tmp_path / "plain"))
    try:
        assert plain.ingest._ocr is None
    finally:
        await plain.close()
