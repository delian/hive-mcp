"""The sync loop's exact bookkeeping: counters, state, thresholds, truncation, what is recorded."""

from __future__ import annotations

import pytest

from hive_mcp.connectors.sync import STATE_NS, STATUS_NS, ConnectorSync, hkey
from hive_mcp.domain.errors import ValidationError
from tests.connectors.test_sync import BOT, SPEC, Fake, titles


@pytest.fixture
def sync(stack):
    b = stack.backend
    return ConnectorSync(stack.ingest, stack.knowledge, b.kv, b.audit, b.clock)


def test_document_keys_are_hashed_to_thirty_two_hex_characters():
    assert len(hkey("https://a.example/doc")) == 32 and hkey("a") != hkey("b")
    assert hkey("a") == hkey("a") and all(c in "0123456789abcdef" for c in hkey("a"))


async def test_the_state_record_names_the_source_version_fingerprint_and_items(stack, sync):
    src = Fake()
    src.docs = {"a": ("alpha text", "v7")}
    await sync.run(SPEC, src)
    state = (await stack.backend.kv.get(STATE_NS, "wiki"))["docs"][hkey("a")]
    assert set(state) == {"key", "ver", "fp", "items"}
    assert state["key"] == "a" and state["ver"] == "v7" and len(state["fp"]) == 64
    assert len(state["items"]) == 1


async def test_counters_add_up_across_several_documents(stack, sync):
    src = Fake()
    src.docs = {k: (f"text of {k}", "v1") for k in "abc"}
    first = await sync.run(SPEC, src)
    assert (first.created, first.unchanged, first.updated) == (3, 0, 0)
    second = await sync.run(SPEC, src)
    assert (second.created, second.unchanged, second.updated) == (0, 3, 0)  # by version
    src.docs = {k: (f"text of {k}", "v2") for k in "abc"}  # touched, not edited: by fingerprint
    third = await sync.run(SPEC, src)
    assert (third.created, third.unchanged, third.updated) == (0, 3, 0)
    src.docs = {k: (f"new words for {k}", "v3") for k in "abc"}
    fourth = await sync.run(SPEC, src)
    assert (fourth.created, fourth.unchanged, fourth.updated) == (0, 0, 3)
    state = (await stack.backend.kv.get(STATE_NS, "wiki"))["docs"]
    assert {s["ver"] for s in state.values()} == {"v3"}


async def test_a_touched_but_identical_document_remembers_its_new_version(stack, sync):
    src = Fake()
    src.docs = {"a": ("alpha", "v1")}
    await sync.run(SPEC, src)
    src.docs = {"a": ("alpha", "v2")}
    await sync.run(SPEC, src)
    assert (await stack.backend.kv.get(STATE_NS, "wiki"))["docs"][hkey("a")]["ver"] == "v2"
    src.loads.clear()
    await sync.run(SPEC, src)
    assert src.loads == []  # v2 is now known: not downloaded again


async def test_failures_are_reported_by_label_cut_to_two_hundred_characters(stack, sync):
    class Long(Fake):
        def _loader(self, key, text):
            async def load():
                raise ValidationError("x" * 500)

            return load

    src = Long()
    src.docs = {"a": ("alpha", "v1")}
    report = await sync.run(SPEC, src)
    assert report.failed == {"a": "x" * 200}
    assert report.created == 0 and report.updated == 0


async def test_an_unexpected_crash_is_logged_and_reported_without_retiring(stack, sync, caplog):
    src = Fake()
    src.docs = {k: (f"text of {k}", "v1") for k in "abcde"}
    await sync.run(SPEC, src)

    class Crashing:
        def documents(self):
            async def gen():
                raise RuntimeError("boom")
                yield  # pragma: no cover

            return gen()

    with caplog.at_level("ERROR", logger="hive_mcp.connectors.sync"):
        report = await sync.run(SPEC, Crashing())
    assert report.aborted == "RuntimeError; nothing retired" and report.retired == 0
    assert [r.getMessage() for r in caplog.records] == ["connector wiki crashed"]
    assert len(await titles(stack)) == 5


async def test_the_source_of_an_item_is_the_connector_and_key_cut_at_four_hundred(stack, sync):
    class Short(Fake):
        def _loader(self, key, text):
            async def load():
                return "doc.md", f"# doc\n\n{text}".encode()

            return load

    src = Short()
    long_key = "k" * 500
    src.docs = {long_key: ("alpha text", "v1")}
    await sync.run(SPEC, src)
    from hive_mcp.domain.item import ItemQuery

    page = await stack.knowledge.browse(BOT, ItemQuery(include_referenced=False, limit=5))
    source = page.items[0].item.source
    assert source == ("connector:wiki:" + long_key)[:400] and len(source) == 400


async def retire_case(stack, sync, total: int, gone: int):
    src = Fake()
    src.docs = {f"d{i}": (f"text of d{i}", "v1") for i in range(total)}
    await sync.run(SPEC, src)
    src.docs = {k: v for i, (k, v) in enumerate(src.docs.items()) if i >= gone}
    return await sync.run(SPEC, src)


async def test_mass_retirement_thresholds_are_exact(stack, sync):
    small = await retire_case(stack, sync, total=4, gone=3)  # too few documents to judge: retire
    assert small.aborted is None and small.retired == 3


async def test_five_documents_with_most_gone_is_refused(stack, sync):
    report = await retire_case(stack, sync, total=5, gone=3)  # the minimum size, 60% gone
    assert report.retired == 0
    assert report.aborted == (
        "3 of 5 documents vanished at once: nothing retired "
        "(run `hive connectors run --force` if that is right)"
    )


async def test_exactly_half_gone_is_allowed(stack, sync):
    report = await retire_case(stack, sync, total=6, gone=3)  # 3 > 3.0 is false
    assert report.aborted is None and report.retired == 3


async def test_what_a_run_leaves_in_status_and_audit_is_complete(stack, sync):
    src = Fake()
    src.docs = {"a": ("alpha", "v1"), "b": ("beta", "v1")}
    src.fail_load = {"b"}
    await sync.run(SPEC, src)
    status = await stack.backend.kv.get(STATUS_NS, "wiki")
    assert status["summary"] == "1 new, 0 updated, 0 unchanged, 0 retired, 1 failed"
    assert status["failed"] == {"b": "download failed"} and "T" in status["at"]
    [event] = [
        r.event
        for r in await stack.backend.audit.list("org1")
        if r.event.action == "connector.sync"
    ]
    assert event.actor_id == "connector:wiki" and event.target_id is None
    assert event.details == {"connector": "wiki", "result": status["summary"]}


async def test_only_twenty_failures_are_kept_in_the_status(stack, sync):
    src = Fake()
    src.docs = {f"d{i}": ("t", "v1") for i in range(25)}
    src.fail_load = set(src.docs)
    report = await sync.run(SPEC, src)
    assert len(report.failed) == 25
    assert len((await stack.backend.kv.get(STATUS_NS, "wiki"))["failed"]) == 20


async def test_a_document_without_a_version_is_always_compared_by_content(stack, sync):
    src = Fake()
    src.docs = {"a": ("alpha", None)}
    await sync.run(SPEC, src)
    src.loads.clear()
    report = await sync.run(SPEC, src)
    assert src.loads == ["a"] and report.unchanged == 1


async def test_an_item_that_cannot_be_archived_is_skipped_with_a_warning(stack, sync, caplog):
    with caplog.at_level("WARNING", logger="hive_mcp.connectors.sync"):
        await sync._archive(BOT, ["no-such-item"])
    assert [r.getMessage() for r in caplog.records] == ["could not archive no-such-item"]


async def _archived_items(stack, sync, texts):
    src = Fake()
    src.docs = {k: (t, "v1") for k, t in texts.items()}
    await sync.run(SPEC, src)
    ids = [
        i
        for s in (await stack.backend.kv.get(STATE_NS, "wiki"))["docs"].values()
        for i in s["items"]
    ]
    return src, ids


async def test_reactivation_skips_active_items_and_revives_every_archived_one(stack, sync):
    from hive_mcp.domain.item import Status

    _, ids = await _archived_items(stack, sync, {"a": "alpha", "b": "beta", "c": "gamma"})
    await sync._archive(BOT, ids[1:])  # the first stays active: it must not stop the loop
    items = [(await stack.knowledge.get(BOT, i)).item for i in ids]
    assert [i.status for i in items] == [Status.ACTIVE, Status.ARCHIVED, Status.ARCHIVED]
    await sync._reactivate(BOT, items)
    after = [(await stack.knowledge.get(BOT, i)).item for i in ids]
    assert [i.status for i in after] == [Status.ACTIVE] * 3
    assert after[0].revision == items[0].revision  # the active one was left alone


async def test_a_reactivation_that_fails_is_logged_and_the_rest_continue(stack, sync, caplog):
    import dataclasses

    from hive_mcp.domain.item import Status

    _, ids = await _archived_items(stack, sync, {"a": "alpha", "b": "beta"})
    await sync._archive(BOT, ids)
    items = [(await stack.knowledge.get(BOT, i)).item for i in ids]
    stale = dataclasses.replace(items[0], revision=items[0].revision + 7)
    with caplog.at_level("WARNING"):
        await sync._reactivate(BOT, [stale, items[1]])
    assert [r.getMessage() for r in caplog.records] == [f"could not reactivate {ids[0]}"]
    assert (await stack.knowledge.get(BOT, ids[0])).item.status is Status.ARCHIVED
    assert (await stack.knowledge.get(BOT, ids[1])).item.status is Status.ACTIVE


async def test_a_document_that_returns_to_an_earlier_text_is_searchable_again(stack, sync):
    from hive_mcp.domain.item import Status

    src = Fake()
    src.docs = {"a": ("alpha original text", "v1")}
    await sync.run(SPEC, src)
    first = (await stack.backend.kv.get(STATE_NS, "wiki"))["docs"][hkey("a")]["items"]
    src.docs = {"a": ("completely different words", "v2")}
    await sync.run(SPEC, src)
    assert (await stack.knowledge.get(BOT, first[0])).item.status is Status.ARCHIVED
    src.docs = {"a": ("alpha original text", "v3")}
    await sync.run(SPEC, src)
    assert (await stack.knowledge.get(BOT, first[0])).item.status is Status.ACTIVE
