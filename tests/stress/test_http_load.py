"""The MCP server end to end over real HTTP: dozens of concurrent clients, per-token rate limits,
and a shutdown in the middle of a write flood that must still leave a verified final backup."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import httpx
import pytest

from hive_mcp.bootstrap import build_services
from hive_mcp.config import Settings
from hive_mcp.domain.item import ItemQuery, Status
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.ops.autobackup import archives
from hive_mcp.ops.backup import restore_backup, verify_backup
from tests.integration.conftest import Live
from tests.stress.support import SCALE, percentile

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.stress
ACCEPT = {"Accept": "application/json, text/event-stream"}
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


@pytest.fixture
def server(tmp_path: Path):
    live = Live(tmp_path / "data")
    live.start()
    yield live
    live.stop()


async def one_client(live: Live, n: int, rounds: int, latencies: list[float]) -> dict:
    org = "acme" if n % 2 == 0 else "globex"
    who = Principal(f"user{n}", org, Role.MEMBER, projects=frozenset({"p1"}))
    token = live.token_for(who, label=f"load-{n}")
    mine: dict[str, str] = {}
    async with live.client(token) as c:
        for i in range(rounds):
            word = f"zz{org}{n}r{i}"
            t = time.perf_counter()
            saved = await c.call_tool(
                "kb_remember", {"title": f"Note {word}", "body": f"{word} narwhal telemetry"}
            )
            latencies.append(time.perf_counter() - t)
            assert not saved.is_error, saved
            mine[word] = saved.structured_content["id"]
        for word, item_id in mine.items():
            t = time.perf_counter()
            hits = (
                await c.call_tool("kb_consult", {"query": word, "limit": 20})
            ).structured_content["results"]
            latencies.append(time.perf_counter() - t)
            assert item_id in [h["id"] for h in hits], f"{who.id} cannot find {word}"
            assert all(h["id"] in mine.values() for h in hits), "someone else's note leaked in"
            got = (await c.call_tool("kb_get", {"id": item_id})).structured_content
            upd = await c.call_tool(
                "kb_update",
                {"id": item_id, "expected_revision": got["revision"], "body": f"{word} edited"},
            )
            assert not upd.is_error and upd.structured_content["revision"] == got["revision"] + 1
    return {"org": org, "user": who.id, "words": mine}


async def test_dozens_of_concurrent_clients_see_only_their_own_notes(server: Live) -> None:
    clients, rounds = 24 * SCALE, 3
    latencies: list[float] = []
    started = time.perf_counter()
    results = await asyncio.gather(
        *(one_client(server, n, rounds, latencies) for n in range(clients))
    )
    elapsed = time.perf_counter() - started
    calls = len(latencies)
    p95 = percentile(latencies, 95)
    print(f"\n[stress] {clients} clients, {calls} timed calls in {elapsed:.1f}s "
          f"({calls / elapsed:.0f}/s), p95 {p95 * 1000:.0f} ms")  # fmt: skip
    assert p95 < 10.0
    # a client can never find another client's words, not even a client of the same organisation
    probe = results[0]
    other = results[1]
    token = server.token_for(
        Principal(probe["user"], probe["org"], Role.MEMBER, projects=frozenset({"p1"}))
    )
    async with server.client(token) as c:
        for word in list(other["words"])[:3] + list(results[2]["words"])[:3]:
            hits = (await c.call_tool("kb_consult", {"query": word})).structured_content["results"]
            foreign = {*other["words"].values(), *results[2]["words"].values()}
            assert not [h for h in hits if h["id"] in foreign]
    # the server saw every write exactly once
    services = await build_services(Settings(data_dir=server.settings.data_dir))
    try:
        expected = {i for r in results for i in r["words"].values()}
        stored = set()
        for r in results:
            who = Principal(r["user"], r["org"], Role.MEMBER)
            page = await services.knowledge.browse(who, ItemQuery(limit=100))
            stored |= {v.item.id for v in page.items}
        assert stored == expected and len(expected) == clients * rounds
    finally:
        await services.close()


def post(live: Live, token: str):
    return httpx.post(
        f"{live.url}/mcp", json=LIST, headers={**ACCEPT, "Authorization": f"Bearer {token}"}
    )


async def test_rate_limit_gives_429_with_retry_after_per_token(tmp_path: Path) -> None:
    live = Live(tmp_path / "data", rate_limit_mcp=60)  # a burst of 10, then 1 per second
    live.start()
    try:
        a = live.token_for(Principal("a", "org1", Role.MEMBER))
        b = live.token_for(Principal("b", "org1", Role.MEMBER))
        async with httpx.AsyncClient(timeout=30) as http:
            headers = {**ACCEPT, "Authorization": f"Bearer {a}"}
            started = time.monotonic()
            responses = await asyncio.gather(
                *(
                    http.post(f"{live.url}/mcp", json=LIST, headers=headers)
                    for _ in range(80 * SCALE)
                )
            )
            elapsed = time.monotonic() - started
            ok = [r for r in responses if r.status_code == 200]
            limited = [r for r in responses if r.status_code == 429]
            assert len(ok) + len(limited) == len(responses), {r.status_code for r in responses}
            assert 10 <= len(ok) <= 10 + int(elapsed) + 2, (len(ok), elapsed)
            for r in limited:
                assert int(r.headers["retry-after"]) >= 1
                assert r.json()["error"] == "rate_limited"
            # a flood on one token leaves the others and the health probes alone
            others = await asyncio.gather(
                *(
                    http.post(
                        f"{live.url}/mcp",
                        json=LIST,
                        headers={**ACCEPT, "Authorization": f"Bearer {b}"},
                    )
                    for _ in range(8)
                ),
                *(http.get(f"{live.url}/healthz") for _ in range(50)),
            )
            assert all(r.status_code == 200 for r in others)
            wait = int(limited[0].headers["retry-after"])
            await asyncio.sleep(min(wait, 2) + 0.1)  # told to wait: the next call is allowed again
            assert (
                await http.post(f"{live.url}/mcp", json=LIST, headers=headers)
            ).status_code == 200
    finally:
        live.stop()


async def test_shutdown_in_the_middle_of_a_write_flood_leaves_a_verified_final_backup(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    share = tmp_path / "share"
    live = Live(
        tmp_path / "disk",
        backup_dir=share,
        backup_every_minutes=60,  # only the first and the final archive happen
        rate_limit_mcp=0,
    )
    live.start()
    acknowledged: dict[str, str] = {}
    failures: list[BaseException] = []
    stop_writing = asyncio.Event()

    async def flood(n: int) -> None:
        who = Principal(f"flood{n}", "acme", Role.MEMBER)
        try:
            async with live.client(live.token_for(who)) as c:
                i = 0
                while not stop_writing.is_set():
                    r = await c.call_tool(
                        "kb_remember", {"title": f"Flood {n}-{i}", "body": f"flood {n} {i} narwhal"}
                    )
                    if not r.is_error:
                        acknowledged[r.structured_content["id"]] = who.id
                    i += 1
        except BaseException as exc:  # the server going away mid-call is the point of the test
            failures.append(exc)

    tasks = [asyncio.create_task(flood(n)) for n in range(16)]
    while len(acknowledged) < 60:
        await asyncio.sleep(0.05)
        assert not live.server.should_exit
    before_stop = dict(acknowledged)
    live.server.handle_exit(15, None)  # SIGTERM semantics with no drain window
    stopped = time.monotonic()
    await asyncio.to_thread(live.thread.join, 40)
    assert not live.thread.is_alive(), "the server did not stop with requests in flight"
    print(f"\n[stress] shut down in {time.monotonic() - stopped:.1f}s with "
          f"{len(acknowledged)} acknowledged writes")  # fmt: skip
    stop_writing.set()
    await asyncio.wait(tasks, timeout=30)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    assert not list(share.glob("*.part")), "a half-written archive was left behind"
    found = archives(share)
    assert found, "no final backup"
    for archive in found:
        verify_backup(archive)  # every archive on the share is complete and readable
    target = Settings(data_dir=tmp_path / "restored")
    restore_backup(target, found[-1])
    services = await build_services(target)
    try:
        stored: set[str] = set()
        for n in range(16):
            who = Principal(f"flood{n}", "acme", Role.MEMBER)
            after = None
            while True:
                from hive_mcp.domain.item import Cursor

                page = await services.knowledge.browse(
                    who, ItemQuery(limit=100, after=after, statuses=frozenset(Status))
                )
                stored |= {v.item.id for v in page.items}
                if page.next_cursor is None:
                    break
                after = Cursor.decode(page.next_cursor)
        # whatever the server acknowledged before it stopped accepting work is in the archive
        assert set(before_stop) <= stored
        assert set(acknowledged) <= stored, "an acknowledged write is missing from the final backup"
    finally:
        await services.close()
