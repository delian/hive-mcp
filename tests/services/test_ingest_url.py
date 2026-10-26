import http.server
import threading

import pytest

from hive_mcp.domain.errors import ValidationError
from hive_mcp.ingest.fetch import FetchPolicy
from hive_mcp.services.ingest import IngestService
from hive_mcp.services.models import ConsultRequest
from tests.conformance.factory import ALICE, BOB

PAGE = b"<html><head><title>Heron census</title></head><body><h1>Counts</h1><p>Seventeen herons were counted.</p></body></html>"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        routes = {
            "/page": (200, "text/html; charset=utf-8", PAGE),
            "/plain": (200, "text/plain", b"plain pelican text"),
            "/image": (200, "video/mp4", b"\x00\x00\x00"),
            "/go": (302, None, b""),
        }
        status, ctype, body = routes.get(self.path, (404, "text/plain", b"missing"))
        self.send_response(status)
        if ctype:
            self.send_header("Content-Type", ctype)
        if status == 302:
            self.send_header("Location", "/page")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def web():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def service(stack, policy):
    return IngestService(stack.knowledge, stack.graph, url_policy=policy)


async def test_a_page_becomes_searchable_knowledge_that_remembers_its_url(stack, web):
    ingest = service(stack, FetchPolicy(allow_private=True))  # the test server is on loopback
    res = await ingest.ingest_url(ALICE, f"{web}/page", tags=("census",))
    item = res.items[0]
    assert item.title.startswith("Heron census") and item.source == f"url:{web}/page"
    assert {"census", "format/html"} <= item.tags
    found = await stack.consult.consult(ALICE, ConsultRequest("herons were counted"))
    assert [m.item_id for m in found.primary] == [item.id]
    again = await ingest.ingest_url(ALICE, f"{web}/page", tags=("census",))
    assert again.created == 0 and again.items[0].id == item.id  # same content: nothing new
    followed = await ingest.ingest_url(ALICE, f"{web}/go", tags=("census",))  # redirect to /page
    assert followed.items[0].source == f"url:{web}/page"
    assert not (
        await stack.consult.consult(BOB, ConsultRequest("herons"))
    ).primary  # private to alice


async def test_text_and_error_cases(stack, web):
    ingest = service(stack, FetchPolicy(allow_private=True))
    assert (await ingest.ingest_url(ALICE, f"{web}/plain")).items[0].body == "plain pelican text"
    for path, message in (("/image", "not a document"), ("/nothing", "HTTP 404")):
        with pytest.raises(ValidationError, match=message):
            await ingest.ingest_url(ALICE, web + path)
    assert (
        await stack.backend.items.list_ids_system("org1", None, 10) != []
    )  # the good one is there


async def test_disabled_by_default_and_private_addresses_refused_unless_allowed(stack, web):
    with pytest.raises(ValidationError, match="not enabled"):
        await service(stack, None).ingest_url(ALICE, f"{web}/page")
    with pytest.raises(ValidationError, match="non-public"):
        await service(stack, FetchPolicy()).ingest_url(ALICE, f"{web}/page")  # 127.0.0.1 is private
    with pytest.raises(ValidationError, match="non-public"):
        await service(stack, FetchPolicy()).ingest_url(
            ALICE, "http://169.254.169.254/latest/meta-data/"
        )
    assert await stack.backend.items.list_ids_system("org1", None, 10) == []
