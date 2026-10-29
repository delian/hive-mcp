"""Cross-area review findings (BUG-OPS-14, BUG-CLI-3)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

from hive_mcp.ops import loops


async def test_bug_ops_14_connector_loop_survives_a_non_hive_error(monkeypatch, caplog) -> None:
    """A connector that raised anything but HiveError (a network error) killed the loop for good."""
    calls = []

    class Kv:
        async def put_if_absent(self, *a, **k):
            return True

    class Connectors:
        async def run(self, spec, connector):
            calls.append(1)
            raise OSError("network down")

    services = SimpleNamespace(backend=SimpleNamespace(kv=Kv()), connectors=Connectors())
    spec = SimpleNamespace(name="x", every_minutes=1)
    monkeypatch.setattr("hive_mcp.connectors.factory.make_connector", lambda s, st: object())
    monkeypatch.setattr(loops, "CONNECTOR_CHECK_SECONDS", 0.01)
    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(loops.connector_loop(services, None, [spec]))  # type: ignore[arg-type]
        await asyncio.sleep(0.15)
        assert not task.done()
        task.cancel()
    assert calls and "connector.failed" in caplog.text


def test_bug_cli_3_eval_set_file_is_private_even_when_it_existed(tmp_path) -> None:
    import os
    import stat

    out = tmp_path / "set.json"
    out.write_text("old")
    out.chmod(0o644)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    os.close(fd)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    src = Path("src/hive_mcp/cli_eval.py").read_text()
    assert "os.fchmod(fd, 0o600)" in src


def test_bug_stress_6_query_terms_are_capped() -> None:
    from hive_mcp.search.tokenize import MAX_QUERY_TERMS, query_terms

    assert len(query_terms(" ".join(f"w{i}" for i in range(30_000)))) == MAX_QUERY_TERMS


def test_bug_stress_3_scanning_a_megabyte_of_image_openers_is_bounded() -> None:
    import time

    from hive_mcp.security.injection import scan

    started = time.perf_counter()
    scan("![a](http://x?" * 100_000)
    assert time.perf_counter() - started < 2


async def test_bug_priv_1_erasing_a_person_removes_their_id_from_old_private_revisions(
    stack,
) -> None:
    """A private note's earlier revisions kept the person's id as scope owner after erasure."""
    from hive_mcp.domain.item import ItemPatch
    from hive_mcp.services.models import RememberRequest
    from tests.conformance.factory import ALICE

    item = (await stack.knowledge.remember(ALICE, RememberRequest("Private", "first draft"))).item
    await stack.backend.items.update(ALICE, item.id, ItemPatch(body="second draft"), 1)
    assert len(await stack.backend.history.versions(ALICE, item.id)) == 2
    changed = await stack.backend.maintenance.pseudonymise_person("org1", ALICE.id, "erased-1")
    assert changed >= 1
    # the old revision no longer names alice as its owner, so even alice cannot read it as hers
    assert [v.revision for v in await stack.backend.history.versions(ALICE, item.id)] == [2]


def test_bug_src_1_item_source_is_length_limited() -> None:
    from datetime import UTC, datetime

    import pytest

    from hive_mcp.domain.errors import ValidationError
    from hive_mcp.domain.item import MAX_SOURCE, Item
    from hive_mcp.domain.scope import Scope, ScopeLevel

    now = datetime.now(UTC)

    def make(source: str) -> Item:
        return Item(
            id="i1", org_id="o", kind="howto", title="t", body="b",  # type: ignore[arg-type]
            scope=Scope(ScopeLevel.PRIVATE, "a"), author_id="a", created_at=now, updated_at=now,
            source=source,
        )  # fmt: skip

    assert make("x" * MAX_SOURCE).source
    with pytest.raises(ValidationError, match="source must be at most"):
        make("x" * (MAX_SOURCE + 1))


async def test_bug_stress_3_an_oversized_body_is_refused_before_sanitising(stack, monkeypatch):
    import pytest

    from hive_mcp.domain.errors import ValidationError
    from hive_mcp.domain.item import MAX_BODY
    from hive_mcp.services import knowledge
    from hive_mcp.services.models import RememberRequest
    from tests.conformance.factory import ALICE

    def boom(*a, **k):
        raise AssertionError("sanitize ran on an oversized body")

    monkeypatch.setattr(knowledge, "sanitize", boom)
    with pytest.raises(ValidationError, match="body must be"):
        await stack.knowledge.remember(ALICE, RememberRequest("t", "x" * (MAX_BODY + 1)))
