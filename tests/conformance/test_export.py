"""The analytics export on every backend: who is left out, paging, checksums, audit."""

import hashlib
import json
import os
import stat
from dataclasses import replace

import pytest

from hive_mcp.domain.edge import Edge
from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.item import ItemPatch, Status
from hive_mcp.domain.scope import ScopeLevel
from hive_mcp.export import ExportOptions, run_export
from hive_mcp.export import service as export_service
from tests.conformance.factory import ALICE, ALICE_PRIVATE, BOB, ORG1, P1, make_item


async def export(backend, out, **flags):
    return await run_export(
        items=backend.items,
        maintenance=backend.maintenance,
        edges=backend.edges,
        audit=backend.audit,
        clock=backend.clock,
        org_id="org1",
        out_dir=out,
        opts=ExportOptions(**flags),
        actor="cli:test",
    )


def read(out, name):
    return [json.loads(line) for line in (out / name).read_text().splitlines()]


async def seed(backend):
    c = backend.clock
    made = {}
    for who, scope, item_id, extra in (
        (ALICE, ALICE_PRIVATE, "n1", {"title": "Secret thought"}),
        (ALICE, P1, "n2", {"tags": frozenset({"x", "y"})}),
        (BOB, ORG1, "n3", {}),
        (BOB, P1, "n4", {}),
    ):
        made[item_id] = make_item(c, who, scope, item_id=item_id, **extra)
        await backend.items.create(who, made[item_id])
        c.advance(seconds=10)
    await backend.items.update(BOB, "n4", ItemPatch(status=Status.ARCHIVED), 1)
    for src, dst in (("n2", "n3"), ("n2", "n1"), ("n4", "n3"), ("n3", "n2")):
        await backend.edges.add(Edge("org1", src, dst, "related", "alice", c.now()))
    return made


async def test_private_notes_and_archived_items_are_left_out_by_default(backend, tmp_path):
    await seed(backend)
    res = await export(backend, tmp_path / "out")
    items = read(res.path, "items.jsonl")
    assert [i["id"] for i in items] == ["n2", "n3"]
    assert items[0]["tags"] == ["x", "y"] and items[0]["scope_level"] == "project"
    assert "Secret thought" not in (res.path / "items.jsonl").read_text()
    edges = read(res.path, "edges.jsonl")
    assert [(e["src"], e["dst"]) for e in edges] == [("n2", "n3"), ("n3", "n2")]
    assert "n1" not in (res.path / "edges.jsonl").read_text()  # no id of a left-out note


async def test_flags_bring_back_private_and_archived(backend, tmp_path):
    await seed(backend)
    both = await export(backend, tmp_path / "a", include_private=True, include_archived=True)
    assert [i["id"] for i in read(both.path, "items.jsonl")] == ["n1", "n2", "n3", "n4"]
    assert both.edges == 4
    private = await export(backend, tmp_path / "b", include_private=True)
    assert private.items == 3 and private.edges == 3  # n4 archived: its link goes too
    archived = await export(backend, tmp_path / "c", include_archived=True)
    assert [i["id"] for i in read(archived.path, "items.jsonl")] == ["n2", "n3", "n4"]


async def test_since_limits_items_and_links_and_other_orgs_are_never_exported(backend, tmp_path):
    made = await seed(backend)
    zed = replace(BOB, id="zed", org_id="org9")
    await backend.items.create(zed, make_item(backend.clock, zed, P1, item_id="z"))
    cutoff = made["n3"].created_at
    res = await export(backend, tmp_path / "s", since=cutoff, include_archived=True)
    assert [i["id"] for i in read(res.path, "items.jsonl")] == ["n3", "n4"]
    assert {i["org"] for i in read(res.path, "items.jsonl")} == {"org1"}


async def test_manifest_checksums_match_and_files_are_private(backend, tmp_path):
    await seed(backend)
    res = await export(backend, tmp_path / "exports" / "out")
    manifest = json.loads((res.path / "manifest.json").read_text())
    assert manifest["schema_version"] == 1 and manifest["org"] == "org1"
    assert manifest["counts"] == {"items": 2, "edges": 2}
    assert manifest["flags"]["include_private"] is False
    assert set(manifest["files"]) == {"items.jsonl", "edges.jsonl"}
    for name, meta in manifest["files"].items():
        data = (res.path / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == meta["sha256"] and len(data) == meta["bytes"]
    for path in res.path.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(res.path.stat().st_mode) == 0o700
    assert [p.name for p in (tmp_path / "exports").iterdir()] == [
        "out"
    ]  # no staging directory left behind


async def test_pseudonymise_is_stable_within_an_export_and_differs_between_exports(
    backend, tmp_path
):
    await seed(backend)
    one = await export(backend, tmp_path / "a", pseudonymise=True, include_private=True)
    items = {i["id"]: i for i in read(one.path, "items.jsonl")}
    assert items["n1"]["author"] == items["n2"]["author"] != "alice"
    assert items["n1"]["scope_owner"] == items["n1"]["author"]  # a private scope names its person
    assert items["n3"]["author"] != items["n2"]["author"]
    assert {e["author"] for e in read(one.path, "edges.jsonl")} == {items["n2"]["author"]}
    text = "".join(p.read_text() for p in one.path.iterdir())
    assert '"alice"' not in text and "bob" not in text.replace("Bob", "")
    two = await export(backend, tmp_path / "b", pseudonymise=True)
    assert read(two.path, "items.jsonl")[0]["author"] != items["n2"]["author"]


async def test_paging_covers_more_than_one_page_with_bounded_batches(
    backend, tmp_path, monkeypatch
):
    monkeypatch.setattr(export_service, "ITEM_PAGE", 3)
    monkeypatch.setattr(export_service, "EDGE_PAGE", 2)
    seen_limits = []
    real = backend.maintenance.find_system

    async def spy(flt, *, after_id=None, limit=100):
        page = await real(flt, after_id=after_id, limit=limit)
        seen_limits.append((limit, len(page)))
        return page

    backend.maintenance.find_system = spy
    for n in range(10):
        await backend.items.create(ALICE, make_item(backend.clock, ALICE, P1, item_id=f"m{n:02d}"))
        if n:
            await backend.edges.add(
                Edge("org1", f"m{n - 1:02d}", f"m{n:02d}", "next", "alice", backend.clock.now())
            )
    res = await export(backend, tmp_path / "out")
    assert [i["id"] for i in read(res.path, "items.jsonl")] == [f"m{n:02d}" for n in range(10)]
    assert res.edges == 9 and len(read(res.path, "edges.jsonl")) == 9
    assert all(limit == 3 and got <= 3 for limit, got in seen_limits) and len(seen_limits) == 5


async def test_it_audits_the_run_without_content_and_refuses_a_non_empty_target(backend, tmp_path):
    await seed(backend)
    await export(backend, tmp_path / "out", include_private=True)
    events = [r.event for r in await backend.audit.list("org1", limit=50)]
    (event,) = [e for e in events if e.action == "export.run"]
    assert event.actor_id == "cli:test"
    assert event.details["include_private"] == "true" and event.details["items"] == "3"
    assert "Secret thought" not in json.dumps(event.details)
    with pytest.raises(ValidationError, match="not an empty directory"):
        await export(backend, tmp_path / "out")
    with pytest.raises(ValidationError, match="format"):
        await export(backend, tmp_path / "x", format="csv")
    assert not (tmp_path / "x").exists()


async def test_a_failure_leaves_no_partial_directory(backend, tmp_path, monkeypatch):
    await seed(backend)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(export_service, "_write_manifest", boom)
    with pytest.raises(OSError, match="disk full"):
        await export(backend, tmp_path / "exports" / "out")
    assert os.listdir(tmp_path / "exports") == []


async def test_parquet_round_trips_when_pyarrow_is_installed(backend, tmp_path):
    pq = pytest.importorskip("pyarrow.parquet")
    await seed(backend)
    res = await export(backend, tmp_path / "out", format="parquet")
    table = pq.read_table(res.path / "items.parquet")
    assert table.column("id").to_pylist() == ["n2", "n3"]
    assert table.column("tags").to_pylist()[0] == ["x", "y"]
    assert table.column("created_at").type.tz == "UTC"
    assert pq.read_table(res.path / "edges.parquet").num_rows == 2
    manifest = json.loads((res.path / "manifest.json").read_text())
    assert manifest["files"]["items.parquet"]["rows"] == 2
    assert ScopeLevel.PROJECT.name.lower() in table.column("scope_level").to_pylist()


async def test_parquet_without_pyarrow_gives_a_clear_error(backend, tmp_path, monkeypatch):
    import builtins

    real = builtins.__import__

    def deny(name, *a, **k):
        if name.startswith("pyarrow"):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", deny)
    with pytest.raises(ValidationError, match=r"hive-mcp\[parquet\]"):
        await export(backend, tmp_path / "out", format="parquet")
    assert not (tmp_path / "out").exists()
