import pytest

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.services.ingest import IngestService
from hive_mcp.services.models import ConsultRequest
from tests.conformance.factory import ALICE, BOB, CAROL

P1 = Scope(ScopeLevel.PROJECT, "p1")
BIG = "\n\n".join(f"# Part {i}\n" + ("word " * 400) for i in range(1, 4))  # ~6KB each


async def test_small_file_becomes_one_private_searchable_doc(stack):
    res = await stack.ingest.ingest(
        ALICE, "radar.md", b"# Radar\nPulse delay gives range.", tags=("domain/rf",)
    )
    assert res.created == 1 and len(res.items) == 1
    item = res.items[0]
    assert item.kind.value == "doc" and item.source == "file:radar.md"
    assert {"domain/rf", "file/radar-md", "format/text"} <= item.tags
    found = await stack.consult.consult(ALICE, ConsultRequest("pulse delay range"))
    assert [m.item_id for m in found.primary] == [item.id]
    assert not (await stack.consult.consult(BOB, ConsultRequest("pulse delay range"))).primary


async def test_reingesting_the_same_file_is_a_no_op_and_changes_make_new_items(stack):
    first = await stack.ingest.ingest(ALICE, "a.txt", b"alpha beta gamma")
    again = await stack.ingest.ingest(ALICE, "a.txt", b"alpha beta gamma")
    assert again.created == 0 and [i.id for i in again.items] == [i.id for i in first.items]
    other_tags = await stack.ingest.ingest(ALICE, "a.txt", b"alpha beta gamma", tags=("x",))
    assert other_tags.created == 1 and other_tags.items[0].id != first.items[0].id
    edited = await stack.ingest.ingest(ALICE, "a.txt", b"alpha beta gamma delta")
    assert edited.created == 1


async def test_long_documents_are_split_and_linked_in_order(stack):
    small = IngestService(stack.knowledge, stack.graph, section_chars=2500)
    res = await small.ingest(ALICE, "book.md", BIG.encode())
    head = res.items[0]
    assert len(res.items) == 3 and head.title.startswith("book.md (1/3)")
    graph = await stack.graph.neighbours(ALICE, head.id, depth=2)
    assert {n.view.item.id for n in graph.nodes} == {i.id for i in res.items}
    edges = {(e.relation, e.src, e.dst) for e in graph.edges}
    first, second, third = (i.id for i in res.items)
    assert edges == {
        ("part_of", second, first),
        ("part_of", third, first),
        ("next", first, second),
        ("next", second, third),
    }
    again = await small.ingest(ALICE, "book.md", BIG.encode())
    assert (
        again.created == 0
        and len((await stack.graph.neighbours(ALICE, head.id, depth=2)).edges) == 4
    )


async def test_project_scope_and_redaction(stack):
    secret = "ghp_" + "a1B2c3D4e5" * 4
    res = await stack.ingest.ingest(
        ALICE, "notes.txt", f"token {secret} for the build".encode(), scope=P1
    )
    assert res.redactions and secret not in res.items[0].body
    assert (
        await stack.consult.consult(BOB, ConsultRequest("build token"))
    ).primary  # project member
    assert not (await stack.consult.consult(CAROL, ConsultRequest("build token"))).primary


async def test_bad_input_is_rejected_before_anything_is_stored(stack):
    for name, data in [("", b"x"), ("a.bin", b"x"), ("a.txt", b"  ")]:
        with pytest.raises(ValidationError):
            await stack.ingest.ingest(ALICE, name, data)
    with pytest.raises(ValidationError, match="MB"):
        await stack.ingest.ingest(ALICE, "a.txt", b"x" * (50 * 1024 * 1024 + 1))
    assert (await stack.backend.items.list_ids_system("org1", None, 10)) == []


async def test_same_file_into_another_scope_is_stored_again_there(stack):
    private = await stack.ingest.ingest(ALICE, "a.txt", b"shared words here")
    shared = await stack.ingest.ingest(ALICE, "a.txt", b"shared words here", scope=P1)
    assert shared.created == 1 and shared.items[0].id != private.items[0].id
    assert shared.items[0].scope == P1 and private.items[0].scope.level is ScopeLevel.PRIVATE


async def test_documents_with_too_many_parts_are_refused_before_storing(stack):
    tiny = IngestService(stack.knowledge, stack.graph, section_chars=30)
    text = "\n\n".join(f"paragraph number {i} here" for i in range(300))
    with pytest.raises(ValidationError, match="too long"):
        await tiny.ingest(ALICE, "long.txt", text.encode())
    assert await stack.backend.items.list_ids_system("org1", None, 10) == []


async def test_parsing_a_document_does_not_freeze_the_event_loop(stack, monkeypatch):
    import asyncio
    import time

    from hive_mcp.ingest.extract import extract_text as real

    def slow(name, data, ocr=None):
        time.sleep(0.4)  # a hostile PDF that takes a while to parse
        return real(name, data, ocr)

    monkeypatch.setattr("hive_mcp.services.ingest.extract_text", slow)
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        await stack.ingest.ingest(ALICE, "slow.txt", b"alpha beta gamma")
    finally:
        beat.cancel()
    assert ticks >= 15  # the loop kept running while the parser worked in a thread


async def test_scanned_documents_and_images_are_ingested_only_when_ocr_is_on(stack):
    import io
    import shutil

    import pytest

    if not (shutil.which("tesseract") and shutil.which("pdftoppm")):
        pytest.skip("tesseract/poppler absent")
    from PIL import Image, ImageDraw, ImageFont

    from hive_mcp.ingest.ocr import OcrConfig

    image = Image.new("RGB", (1100, 200), "white")
    ImageDraw.Draw(image).text(
        (40, 40),
        "Calibration interval is twelve months",
        fill="black",
        font=ImageFont.load_default(size=44),
    )
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    with pytest.raises(ValidationError, match="HIVE_OCR"):
        await stack.ingest.ingest(ALICE, "scan.png", buffer.getvalue())
    reading = IngestService(stack.knowledge, stack.graph, ocr=OcrConfig())
    result = await reading.ingest(ALICE, "scan.png", buffer.getvalue())
    item = result.items[0]
    assert "format/image-ocr" in item.tags and "twelve months" in item.body.lower()
