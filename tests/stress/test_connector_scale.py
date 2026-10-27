"""Connector sync with thousands of documents: incremental runs, edits, retirement, and the
mass-retire guard that stops an empty or broken listing from wiping the knowledge base."""

from __future__ import annotations

import time

import pytest

from hive_mcp.connectors.base import Document
from hive_mcp.connectors.spec import ConnectorSpec
from hive_mcp.connectors.sync import STATE_NS, ConnectorSync
from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.item import Cursor, ItemQuery, Status
from hive_mcp.domain.principal import Principal, Role
from tests.stress.support import SCALE

pytestmark = pytest.mark.stress

SPEC = ConnectorSpec("wiki", "urls", "org1", "bot", "p1", ("source/wiki",), 60)
BOT = Principal("bot", "org1", Role.MEMBER, frozenset(), frozenset({"p1"}))


class Source:
    """A document source the test edits between runs."""

    def __init__(self, count: int) -> None:
        self.docs: dict[str, tuple[str, str]] = {  # key -> (text, version)
            f"doc{i:05d}": (f"content of document {i} about pelican {i % 13}", "v1")
            for i in range(count)
        }
        self.loads = 0
        self.fail_after: int | None = None  # the listing raises after this many documents

    def documents(self):
        async def gen():
            for n, (key, (text, version)) in enumerate(list(self.docs.items())):
                if self.fail_after is not None and n >= self.fail_after:
                    raise ValidationError("source unreachable")
                yield Document(key, key, self._loader(key, text), version)

        return gen()

    def _loader(self, key: str, text: str):
        async def load() -> tuple[str, bytes]:
            self.loads += 1
            return f"{key}.md", f"# {key}\n\n{text}".encode()

        return load


@pytest.fixture
def sync(stack) -> ConnectorSync:
    b = stack.backend
    return ConnectorSync(stack.ingest, stack.knowledge, b.kv, b.audit, b.clock)


async def count(stack, status: Status = Status.ACTIVE) -> int:
    total, after = 0, None
    while True:
        page = await stack.knowledge.browse(
            BOT,
            ItemQuery(
                statuses=frozenset({status}), include_referenced=False, limit=100, after=after
            ),
        )
        total += len(page.items)
        if page.next_cursor is None:
            return total
        after = Cursor.decode(page.next_cursor)


def documents_for(stack) -> int:
    return 300 * SCALE if type(stack.backend).__name__ == "PostgresBackend" else 900 * SCALE


async def test_thousands_of_documents_sync_incrementally_edit_and_retire(stack, sync) -> None:
    n = documents_for(stack)
    src = Source(n)
    t0 = time.perf_counter()
    report = await sync.run(SPEC, src)
    first = time.perf_counter() - t0
    assert (report.created, report.updated, report.retired, report.failed) == (n, 0, 0, {})
    assert await count(stack) == n

    src.loads = 0
    t0 = time.perf_counter()
    report = await sync.run(SPEC, src)  # nothing changed: no downloads at all
    again = time.perf_counter() - t0
    assert (report.unchanged, report.created, report.updated) == (n, 0, 0) and src.loads == 0
    print(f"\n[stress] {n} documents: first sync {first:.1f}s, unchanged re-sync {again:.2f}s")
    assert again < 20

    edited = list(src.docs)[::10]  # a tenth change
    for key in edited:
        src.docs[key] = (src.docs[key][0] + " revised", "v2")
    report = await sync.run(SPEC, src)
    assert (report.updated, report.created, report.unchanged) == (len(edited), 0, n - len(edited))
    assert await count(stack) == n, "an edited document must replace, not add"
    assert await count(stack, Status.ARCHIVED) == len(edited)

    for key in list(src.docs)[1::10][: n // 10]:  # a tenth disappear: below the guard
        del src.docs[key]
    gone = n - len(src.docs)
    report = await sync.run(SPEC, src)
    assert report.retired == gone and report.aborted is None
    assert await count(stack) == len(src.docs)


async def test_the_mass_retire_guard_trips_exactly_above_half_and_can_be_overridden(
    stack, sync
) -> None:
    n = documents_for(stack) // 3
    n -= n % 2  # an even number so that exactly half is possible
    src = Source(n)
    await sync.run(SPEC, src)
    keys = list(src.docs)
    backup = dict(src.docs)

    for key in keys[: n // 2 + 1]:  # one more than half vanishes
        del src.docs[key]
    blocked = await sync.run(SPEC, src)
    assert blocked.retired == 0 and "vanished at once" in (blocked.aborted or "")
    assert await count(stack) == n, "the guard must keep every document"
    state = await stack.backend.kv.get(STATE_NS, SPEC.name)
    assert len(state["docs"]) == n, "the remembered state still knows every document"

    src.docs = dict(backup)  # they come back: nothing was lost, nothing is duplicated
    back = await sync.run(SPEC, src)
    assert (back.created, back.retired, back.updated) == (0, 0, 0) and back.unchanged == n
    assert await count(stack) == n

    for key in keys[: n // 2]:  # exactly half vanishes: allowed
        del src.docs[key]
    half = await sync.run(SPEC, src)
    assert half.retired == n // 2 and half.aborted is None
    assert await count(stack) == n - n // 2

    src.docs = {}  # an empty listing: blocked again, unless forced
    empty = await sync.run(SPEC, src)
    assert empty.retired == 0 and empty.aborted and await count(stack) == n - n // 2
    forced = await sync.run(SPEC, src, allow_mass_retire=True)
    assert forced.retired == n - n // 2 and await count(stack) == 0


async def test_a_listing_that_breaks_half_way_retires_nothing(stack, sync) -> None:
    n = documents_for(stack) // 3
    src = Source(n)
    await sync.run(SPEC, src)
    src.fail_after = n // 2  # the first half is listed, then the source errors out
    for key in list(src.docs)[: n // 4]:
        src.docs[key] = (src.docs[key][0] + " edited", "v2")  # edits seen before the error apply
    report = await sync.run(SPEC, src)
    assert report.retired == 0 and "nothing retired" in (report.aborted or "")
    assert await count(stack) == n, "documents beyond the failure point must not be retired"
    src.fail_after = None
    healed = await sync.run(SPEC, src)
    assert healed.aborted is None and healed.retired == 0 and await count(stack) == n


async def test_two_simultaneous_syncs_of_one_connector_do_not_duplicate_documents(
    stack, sync
) -> None:
    import asyncio

    n = 60
    src = Source(n)
    reports = await asyncio.gather(sync.run(SPEC, src), sync.run(SPEC, src))
    assert sum(r.created for r in reports) >= n
    assert await count(stack) == n, f"{await count(stack)} active items for {n} documents"
