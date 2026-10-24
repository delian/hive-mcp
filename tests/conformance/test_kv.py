import asyncio

NS = "oauth"


async def test_roundtrip_overwrite_delete_and_namespaces(backend):
    await backend.kv.put(NS, "k", {"a": 1, "nested": {"x": [1, 2]}})
    assert await backend.kv.get(NS, "k") == {"a": 1, "nested": {"x": [1, 2]}}
    await backend.kv.put(NS, "k", {"a": 2})
    assert await backend.kv.get(NS, "k") == {"a": 2}
    assert await backend.kv.get("other", "k") is None  # namespaces are separate
    await backend.kv.delete(NS, "k")
    await backend.kv.delete(NS, "k")  # idempotent
    assert await backend.kv.get(NS, "k") is None


async def test_returned_values_are_copies(backend):
    await backend.kv.put(NS, "k", {"list": [1]})
    first = await backend.kv.get(NS, "k")
    first["list"].append(99)
    assert await backend.kv.get(NS, "k") == {"list": [1]}


async def test_ttl_expiry_hides_records_everywhere(backend):
    await backend.kv.put(NS, "short", {"v": 1}, ttl_seconds=10)
    await backend.kv.put(NS, "forever", {"v": 2})
    backend.clock.advance(seconds=9)
    assert await backend.kv.get(NS, "short") == {"v": 1}
    backend.clock.advance(seconds=2)
    assert await backend.kv.get(NS, "short") is None
    assert await backend.kv.take(NS, "short") is None
    assert await backend.kv.get(NS, "forever") == {"v": 2}


async def test_put_if_absent_respects_expiry(backend):
    assert await backend.kv.put_if_absent(NS, "k", {"v": 1}, ttl_seconds=5)
    assert not await backend.kv.put_if_absent(NS, "k", {"v": 2})
    assert await backend.kv.get(NS, "k") == {"v": 1}
    backend.clock.advance(seconds=6)
    assert await backend.kv.put_if_absent(NS, "k", {"v": 3})  # the expired record no longer blocks
    assert await backend.kv.get(NS, "k") == {"v": 3}


async def test_take_is_get_and_delete(backend):
    await backend.kv.put(NS, "code", {"user": "alice"})
    assert await backend.kv.take(NS, "code") == {"user": "alice"}
    assert await backend.kv.take(NS, "code") is None and await backend.kv.get(NS, "code") is None


async def test_concurrent_take_has_exactly_one_winner(backend):
    for round_no in range(20):
        key = f"code-{round_no}"
        await backend.kv.put(NS, key, {"n": round_no})
        results = await asyncio.gather(*(backend.kv.take(NS, key) for _ in range(8)))
        assert sum(r is not None for r in results) == 1, f"replay possible at round {round_no}"


async def test_concurrent_put_if_absent_has_exactly_one_winner(backend):
    for round_no in range(20):
        key = f"client-{round_no}"
        results = await asyncio.gather(
            *(backend.kv.put_if_absent(NS, key, {"by": i}) for i in range(8))
        )
        assert sum(results) == 1, f"duplicate registration at round {round_no}"


async def test_purge_removes_only_expired_records(backend):
    await backend.kv.put(NS, "a", {"v": 1}, ttl_seconds=5)
    await backend.kv.put(NS, "b", {"v": 2}, ttl_seconds=100)
    await backend.kv.put(NS, "c", {"v": 3})
    backend.clock.advance(seconds=10)
    assert await backend.kv.purge_expired() == 1
    assert await backend.kv.get(NS, "b") == {"v": 2} and await backend.kv.get(NS, "c") == {"v": 3}


async def test_unicode_and_large_values(backend):
    value = {"name": "Радар 雷达 🛰️", "blob": "x" * 200_000}
    await backend.kv.put(NS, "big", value)
    assert await backend.kv.get(NS, "big") == value
