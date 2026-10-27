"""Text processing on hostile input: megabyte lines, pathological regex inputs, deep nesting, zip
bombs. Everything must finish in bounded time and raise nothing but HiveError subclasses."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import io
import json
import signal
import time
import zipfile
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from hive_mcp.domain.errors import HiveError
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.tags import normalize_tag, normalize_tags
from hive_mcp.ingest.chunker import chunk_text
from hive_mcp.ingest.extract import extract_text
from hive_mcp.ingest.sections import split_sections
from hive_mcp.search.tokenize import query_terms, tokenize
from hive_mcp.security.injection import scan, strip_invisible
from hive_mcp.security.redact import redact
from hive_mcp.security.sanitize import sanitize
from hive_mcp.services.models import ConsultRequest, RememberRequest
from hive_mcp.slack.handlers import MENTION, mrkdwn
from hive_mcp.teams.extension_cards import plain
from hive_mcp.teams.handlers import clean_text
from tests.conformance.factory import ALICE
from tests.integration.conftest import Live
from tests.stress.support import SCALE

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytestmark = pytest.mark.stress
BOUND = 5.0  # seconds of CPU a single call may take (about 10x the slowest healthy call)
SIZE = min(150_000 * SCALE, 2_000_000)
MEGABYTE = 1_000_000
XFAIL_BOUND = 1.0  # the known-bad inputs take 25+ s: stop them early


class TooSlow(AssertionError):
    pass


@contextlib.contextmanager
def cpu_limit(seconds: float) -> Iterator[None]:
    """Abort a runaway call (the regex engine checks for signals). Uses the CPU-time timer, so it
    does not collide with pytest-timeout's wall-clock alarm."""

    def fire(*_: object) -> None:
        raise TooSlow(f"still running after {seconds:.0f}s of CPU time")

    old = signal.signal(signal.SIGVTALRM, fire)
    signal.setitimer(signal.ITIMER_VIRTUAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_VIRTUAL, 0)
        signal.signal(signal.SIGVTALRM, old)


def attempt(fn: Callable[[], Any]) -> tuple[float, BaseException | None]:
    """(seconds, the exception that is not allowed or None). HiveError is the allowed one."""
    started = time.perf_counter()
    try:
        with cpu_limit(BOUND * 2):
            fn()
    except HiveError:
        pass
    except BaseException as exc:
        return time.perf_counter() - started, exc
    return time.perf_counter() - started, None


# ------------------------------------------------------------------------------------ corpus
@functools.cache
def corpus(n: int) -> dict[str, str]:
    return {
        "a-line": "a" * n,
        "spaces": " " * n,
        "newlines": "\n" * n,
        "blank-lines-with-tabs": "\n\t " * (n // 3),
        "ignore-ignore": "ignore " * (n // 7),
        "ignore-all-instructions": ("ignore " + "all " * 10) * (n // 50),
        "send-send": "send " * (n // 5),
        "send-token-run": ("send " + "x " * 30 + "token ") * (n // 80),
        "system-colon": "system: " * (n // 8),
        "system-newline": "system\n" * (n // 7),
        "do-not": "do not " * (n // 7),
        "do-not-tell": ("do not " + "a" * 15 + " tell " + "b" * 15) * (n // 60),
        "image-open": "![" + "a" * n,
        "bearer-bearer": "bearer " * (n // 7),
        "bearer-long": "Bearer " + "A" * n,
        "password-password": "password=" * (n // 9),
        "password-long": "password=" + "x" * n,
        "api-key-spaces": "api_key" + " " * n,
        "url-userinfo": "://" + "a" * n,
        "url-userinfo-colon": "://a:" + "b" * n,
        "url-userinfo-repeat": "://a:b" * (n // 6),
        "akia": "AKIA" * (n // 4),
        "akia-long": "AKIA" + "A" * n,
        "jwt-start": "eyJ" + "a" * n,
        "pem-begin-repeat": "-----BEGIN PRIVATE KEY-----\n" * (n // 28),
        "pem-long": "-----BEGIN PRIVATE KEY-----" + "a" * n,
        "sk-repeat": "sk-" * (n // 3),
        "github-pat": "ghp_" + "a" * n,
        "azure-key": "AccountKey=" * (n // 11),
        "angle-open": "<" * n,
        "angle-a": "<a " * (n // 3),
        "mention-tags": "<@U1|" * (n // 5),
        "html-tags": "<b>" * (n // 3),
        "entities": "&amp;" * (n // 5),
        "zero-width": "\u200b" * n,
        "tag-chars": "\U000e0041" * (n // 2),
        "bidi": "‮" * n,
        "heading-hashes": "# " * (n // 2),
        "heading-lines": "#  x\n" * (n // 5),
        "heading-spaces": "# " + " " * n,
        "words": "w " * (n // 2),
        "tag-path": "a/" * (n // 2),
        "combining": "é" * (n // 2),
        "ligatures": "ﬃ" * (n // 2),
        "mixed-nul": "ab\x00" * (n // 3),
    }


def text_functions() -> dict[str, Callable[[str], Any]]:
    return {
        "injection.scan": scan,
        "injection.strip_invisible": strip_invisible,
        "redact": redact,
        "sanitize": lambda t: sanitize(t[:200], t),
        "teams.clean_text": clean_text,
        "teams.cards.plain": plain,
        "slack.mrkdwn": mrkdwn,
        "slack.mention": lambda t: MENTION.sub("", t),
        "chunk_text": chunk_text,
        "split_sections": split_sections,
        "tokenize": tokenize,
        "query_terms": query_terms,
        "extract.txt": lambda t: extract_text("a.txt", t.encode()),
        "extract.md": lambda t: extract_text("a.md", t.encode()),
        "extract.html": lambda t: extract_text("a.html", t.encode()),
        "extract.xml": lambda t: extract_text("a.xml", t.encode()),
        "extract.json": lambda t: extract_text("a.json", t.encode()),
        "normalize_tag": normalize_tag,
        "normalize_tags": lambda t: normalize_tags([t, t[::-1]]),
    }


# Known product bugs, covered by the xfail tests at the bottom (the sweep skips them so that it
# keeps guarding everything else; remove an entry when its bug is fixed).
KNOWN = {
    ("teams.clean_text", "angle-open"),
    ("slack.mention", "mention-tags"),
}


@pytest.mark.parametrize("name", sorted(text_functions()))
def test_every_text_function_survives_the_hostile_corpus(name: str) -> None:
    fn = text_functions()[name]
    problems, slowest = [], (0.0, "")
    for label, text in corpus(SIZE).items():
        if (name, label) in KNOWN:
            continue
        seconds, exc = attempt(lambda text=text: fn(text))
        slowest = max(slowest, (seconds, label))
        if exc is not None:
            problems.append(f"{label}: {type(exc).__name__} {str(exc)[:80]}")
        elif seconds > BOUND:
            problems.append(f"{label}: took {seconds:.1f}s")
    assert not problems, problems
    print(f"\n[stress] {name}: slowest {slowest[1]} {slowest[0]:.2f}s at {SIZE} chars")


def test_a_megabyte_line_through_the_whole_write_path_stays_linear() -> None:
    """Plain megabyte inputs (no tricks) through the writers' cleaning pipeline."""
    for label in ("a-line", "spaces", "words", "newlines", "zero-width"):
        text = corpus(MEGABYTE)[label]
        seconds, exc = attempt(
            lambda text=text: (sanitize("t", text), chunk_text(text), tokenize(text))
        )
        assert exc is None and seconds < BOUND, (label, seconds, exc)


# ---------------------------------------------------------------------------- archives and XML
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def zipped(members: dict[str, bytes | str], level: int = 6) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=level) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def docx(body: str) -> bytes:
    return zipped({"word/document.xml": f"<w:document {W}><w:body>{body}</w:body></w:document>"})


def sheet_xml(rows: int, cols: int = 1) -> str:
    cells = lambda r: "".join(  # noqa: E731
        f'<c r="{chr(65 + c)}{r}" t="inlineStr"><is><t>v{r}x{c}</t></is></c>' for c in range(cols)
    )
    body = "".join(f'<row r="{r}">{cells(r)}</row>' for r in range(1, rows + 1))
    return f"<worksheet {S}><sheetData>{body}</sheetData></worksheet>"


@functools.cache
def office_cases() -> dict[str, tuple[str, bytes]]:
    depth = 3000
    p = "<w:p><w:r><w:t>x</w:t></w:r></w:p>"
    return {
        "zip bomb: one 40 MB part": (
            "a.docx",
            zipped({"word/document.xml": b"\0" * 40_000_000}, 9),
        ),
        "zip bomb: 6000 members": ("a.docx", zipped({f"f{i}": b"x" for i in range(6000)})),
        "zip bomb: 5 parts of 15 MB": (
            "a.xlsx",
            zipped(
                {f"xl/worksheets/sheet{i}.xml": b"<a/>" + b" " * 15_000_000 for i in range(5)}, 9
            ),
        ),
        "billion laughs": (
            "a.docx",
            zipped(
                {
                    "word/document.xml": '<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa">'
                    '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
                    f"<w:document {W}><w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body></w:document>"
                }
            ),
        ),
        "external entity": (
            "a.docx",
            zipped(
                {
                    "word/document.xml": '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                    f"<w:document {W}><w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>"
                }
            ),
        ),
        "nested paragraphs 100000 deep": (
            "a.docx",
            docx("<w:p>" * 100_000 + "<w:r><w:t>x</w:t></w:r>" + "</w:p>" * 100_000),
        ),
        "nested tables 3000 deep": (
            "a.docx",
            docx("<w:tbl><w:tr><w:tc>" * depth + p + "</w:tc></w:tr></w:tbl>" * depth),
        ),
        "nested content controls 3000 deep": (
            "a.docx",
            docx("<w:sdt><w:sdtContent>" * depth + p + "</w:sdtContent></w:sdt>" * depth),
        ),
        "400k paragraphs": ("a.docx", docx(p * 400_000)),
        "xlsx 100k rows": ("a.xlsx", zipped({"xl/worksheets/sheet1.xml": sheet_xml(100_000)})),
        "xlsx cell in column XFD": (
            "a.xlsx",
            zipped(
                {
                    "xl/worksheets/sheet1.xml": f"<worksheet {S}><sheetData><row r='1'>"
                    "<c r='XFD1' t='inlineStr'><is><t>v</t></is></c></row></sheetData></worksheet>"
                }
            ),
        ),
        "xlsx cell far outside": (
            "a.xlsx",
            zipped(
                {
                    "xl/worksheets/sheet1.xml": f"<worksheet {S}><sheetData><row r='1'>"
                    "<c r='ZZZZZZZZZ1' t='inlineStr'><is><t>v</t></is></c></row></sheetData></worksheet>"
                }
            ),
        ),
        "xlsx shared-string index bomb": (
            "a.xlsx",
            zipped(
                {
                    "xl/worksheets/sheet1.xml": f"<worksheet {S}><sheetData><row r='1'>"
                    "<c r='A1' t='s'><v>99999999999999999999</v></c></row></sheetData></worksheet>",
                    "xl/sharedStrings.xml": f"<sst {S}><si><t>a</t></si></sst>",
                }
            ),
        ),
        "pptx broken xml": ("a.pptx", zipped({"ppt/slides/slide1.xml": "<<<<"})),
        "not a zip": ("a.docx", b"PK\x03\x04" + b"junk" * 1000),
        "truncated zip": ("a.docx", docx(p * 100)[:-30]),
        "random bytes": ("a.xlsx", bytes(range(256)) * 4000),
        "pdf junk": ("a.pdf", b"%PDF-1.4\n" + b"x" * MEGABYTE),
        "pdf repeated objects": (
            "a.pdf",
            b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n" * 20_000,
        ),
        "pdf with many page markers": (
            "a.pdf",
            b"%PDF-1.4\n" + b"<< /Type /Page >>\n" * 100_000 + b"%%EOF",
        ),
        "html nested 200000 deep": ("a.html", ("<div>" * 200_000 + "x").encode()),
        "html entity flood": ("a.html", b"<p>" + b"&#x1F600;" * 500_000),
        "html unterminated script": ("a.html", b"<script>" + b"<" * MEGABYTE),
        "html unterminated comment": ("a.html", b"<!--" + b"-" * MEGABYTE),
        "html huge attribute": ("a.html", b"<p title='" + b"x" * MEGABYTE),
        "utf-16 with bom": ("a.txt", b"\xff\xfe" + b"a\x00" * 500_000),
        "utf-16 odd length": ("a.txt", b"\xff\xfe" + b"a\x00" * 500_000 + b"x"),
        "latin-1 high bytes": ("a.txt", bytes(range(128, 256)) * 8000),
        "json 500000 deep (as text)": ("a.json", b"[" * 500_000 + b"]" * 500_000),
        "xml 300000 deep (as text)": ("a.xml", b"<a>" * 300_000),
    }


@pytest.mark.parametrize("case", sorted(office_cases()))
def test_extractors_survive_archives_bombs_and_deep_nesting(case: str) -> None:
    name, data = office_cases()[case]
    seconds, exc = attempt(lambda: extract_text(name, data))
    assert exc is None, f"{type(exc).__name__}: {exc}"
    assert seconds < BOUND, f"{seconds:.1f}s"


# --------------------------------------------------------------------------------- write path
ADVERSARIAL_WRITES = {
    "nul": ("t", "a\x00b"),
    "lone surrogate": ("t", "abc \ud800 def"),
    "surrogate title": ("t\udfff", "body"),
    "emoji": ("t \U0001f600", "x \U0001f600" * 1000),
    "bidi override": ("t", "abc ‮ def"),
    "control chars": ("t", "a\x01\x02\x7f\x85b"),
    "non-characters": ("t", "￿￾\U0010ffff"),
    "title too long": ("t" * 201, "b"),
    "empty": ("", "b"),
    "whitespace only": ("   ", "   "),
    "body at the limit": ("t", "x" * 200_000),
    "body one over": ("t", "x" * 200_001),
    "megabyte body": ("t", "lorem ipsum " * 100_000),
    "ten megabyte body": ("t", "y" * 10_000_000),
    "all secrets": (
        "t",
        "AKIA" + "A" * 16 + " ghp_" + "b" * 40 + " password=hunter2hunter2 " * 100,
    ),
}


async def test_the_write_path_rejects_or_stores_hostile_text_without_crashing(stack) -> None:
    for label, (title, body) in ADVERSARIAL_WRITES.items():
        started = time.perf_counter()
        try:
            res = await asyncio.wait_for(
                stack.knowledge.remember(ALICE, RememberRequest(title, body)), 60
            )
        except HiveError:
            pass
        else:
            got = (await stack.knowledge.get(ALICE, res.item.id)).item
            assert got.id == res.item.id and "\x00" not in got.body, label
        assert time.perf_counter() - started < BOUND * 4, label


async def test_search_survives_hostile_queries(stack) -> None:
    await stack.knowledge.remember(ALICE, RememberRequest("pelican", "pelican body w1 w2"))
    queries = {
        "nul": "a\x00b pelican",
        "lone surrogate": "a\ud800b pelican",
        "operators": '" OR NEAR( ) * ^ - + AND NOT pelican',
        "quotes": '"' * 100_000,
        "tsquery syntax": "& | ! ( ) <-> :* ' \\ pelican",
        "parentheses": "(" * 100_000,
        "megabyte token": "a" * MEGABYTE,
        "a million words": "a " * 500_000,
        "emoji": "\U0001f600" * 100_000,
        "5000 distinct words": " ".join(f"w{i}" for i in range(5000)),
    }
    for label, query in queries.items():
        seconds, exc = await _timed(stack.consult.consult(ALICE, ConsultRequest(query)))
        assert exc is None or isinstance(exc, HiveError), (label, exc)
        assert seconds < BOUND, (label, seconds)


async def _timed(coro) -> tuple[float, BaseException | None]:
    started = time.perf_counter()
    try:
        await coro
    except BaseException as exc:
        return time.perf_counter() - started, exc
    return time.perf_counter() - started, None


# ------------------------------------------------------------------------------ over HTTP
@pytest.fixture
def server(tmp_path):
    live = Live(tmp_path / "data")
    live.start()
    yield live
    live.stop()


def test_hostile_json_bodies_get_4xx_and_the_server_stays_healthy(server: Live) -> None:
    token = server.token_for(Principal("a", "o", Role.MEMBER))
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
    bodies = {
        "array 200000 deep": "[" * 200_000 + "]" * 200_000,
        "object 100000 deep": '{"a":' * 100_000 + "1" + "}" * 100_000,
        "arguments 50000 deep": json.dumps(call)[:-1]
        + ',"params":{"name":"kb_consult","arguments":{"query":'
        + "[" * 50_000
        + "]" * 50_000
        + "}}}",
        "unterminated": '{"jsonrpc":"2.0","id":1,"method":"tools/list"',
        "binary": bytes(range(256)).decode("latin-1") * 1000,
        "lone surrogate escape": '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
        '{"name":"kb_consult","arguments":{"query":"\\ud800"}}}',
        "huge number": '{"jsonrpc":"2.0","id":' + "9" * 100_000 + ',"method":"tools/list"}',
    }
    with httpx.Client(timeout=60) as http:
        for label, body in bodies.items():
            started = time.perf_counter()
            r = http.post(
                f"{server.url}/mcp", content=body.encode("utf-8", "surrogatepass"), headers=headers
            )
            assert r.status_code < 500, (label, r.status_code, r.text[:100])
            assert time.perf_counter() - started < BOUND * 2, label
        ok = http.post(
            f"{server.url}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers=headers,
        )
        assert ok.status_code == 200 and http.get(f"{server.url}/healthz").status_code == 200


# ======================================================================== known product bugs
@pytest.mark.xfail(
    strict=True,
    reason="BUG-STRESS-3: the markdown-image-exfil rule backtracks quadratically (cubically on "
    "repeated openers): scanning a megabyte takes minutes and blocks the event loop; kb_remember "
    "sanitises BEFORE it checks the 200,000-character limit "
    "(src/hive_mcp/security/injection.py:38-41, src/hive_mcp/services/knowledge.py:75)",
)
def test_scan_and_sanitize_of_a_megabyte_of_image_openers_are_bounded() -> None:
    text = "![a](http://x?" * (MEGABYTE // 14)
    for fn in (scan, lambda t: sanitize("t", t)):
        started = time.perf_counter()
        with cpu_limit(XFAIL_BOUND):
            fn(text)
        assert time.perf_counter() - started < XFAIL_BOUND


@pytest.mark.xfail(
    strict=True,
    reason="BUG-STRESS-4: teams clean_text: AT_TAG '<at[^>]*>.*?</at>' (DOTALL) is quadratic "
    "when '<at>' is not closed; a 200 KB message body (the route allows 256 KB) takes ~25 s "
    "of CPU on the event loop (src/hive_mcp/teams/handlers.py:36)",
)
def test_teams_message_cleaning_is_bounded_for_unclosed_mentions() -> None:
    text = "<at>" * 50_000
    started = time.perf_counter()
    with cpu_limit(XFAIL_BOUND):
        clean_text(text)
    assert time.perf_counter() - started < XFAIL_BOUND


@pytest.mark.xfail(
    strict=True,
    reason="BUG-STRESS-5: TeamsRoutes.messages parses the JSON body before checking the token and "
    "catches only ValueError, so a 100 KB body of '[' raises RecursionError (unauthenticated "
    "500) instead of 400 (src/hive_mcp/teams/routes.py:50)",
)
async def test_teams_route_answers_deeply_nested_json_with_a_client_error() -> None:
    from hive_mcp.integrations.config import IntegrationConfig
    from hive_mcp.teams.routes import TeamsRoutes

    config = IntegrationConfig("teams", True, "tenant", "app", secrets={"app_password": "x"})

    async def get_config() -> IntegrationConfig:
        return config

    class Request:
        headers: dict[str, str] = {}  # noqa: RUF012

        async def body(self) -> bytes:
            return b"[" * 100_000 + b"]" * 100_000

    routes = TeamsRoutes(get_config, lambda c: (object(), object()), lambda: None)  # type: ignore[arg-type]
    response = await routes.messages(Request())  # type: ignore[arg-type]
    assert 400 <= response.status_code < 500


@pytest.mark.xfail(
    strict=True,
    reason="BUG-STRESS-6: kb_consult has no cap on the number of query terms; 30,000 distinct "
    "words take ~1.5 s (quadratic) on SQLite (one serialised connection: every other caller waits) and fail "
    "with a misleading retryable StorageBusy on Postgres (StatementTooComplex: stack depth) "
    "(src/hive_mcp/search/tokenize.py query_terms, adapters/sqlite/text.py:54, "
    "adapters/postgres/text.py:40)",
)
async def test_a_query_of_thirty_thousand_distinct_words_is_answered_quickly(stack) -> None:
    if type(stack.backend).__name__ == "MemoryBackend":
        pytest.skip("the in-memory index has no query planner to overload")
    await stack.knowledge.remember(ALICE, RememberRequest("pelican", "pelican body w1 w2"))
    query = " ".join(f"w{i}" for i in range(30_000))
    started = time.perf_counter()
    await stack.consult.consult(ALICE, ConsultRequest(query))
    assert time.perf_counter() - started < 0.8


@pytest.mark.xfail(
    strict=True,
    reason="BUG-STRESS-7 (low: real Slack escapes '<' in user text): the Slack MENTION regex "
    "'<@[A-Z0-9]+(?:\\|[^>]*)?>' is quadratic on unclosed '<@U1|' runs; 200 KB takes ~28 s, "
    "the 64 KB the route allows ~3 s (src/hive_mcp/slack/handlers.py:44)",
)
def test_slack_mention_removal_is_bounded_for_unclosed_mentions() -> None:
    text = "<@U1|" * (200_000 // 5)
    started = time.perf_counter()
    with cpu_limit(XFAIL_BOUND):
        MENTION.sub("", text)
    assert time.perf_counter() - started < XFAIL_BOUND
