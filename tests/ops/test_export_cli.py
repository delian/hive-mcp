"""`hive export` end to end against a SQLite data directory."""

from __future__ import annotations

import asyncio
import json
import stat

import pytest
from typer.testing import CliRunner

from hive_mcp.bootstrap import build_services
from hive_mcp.cli import cli
from hive_mcp.config import Settings
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.services.models import RememberRequest

runner = CliRunner()
ANN = Principal("ann@acme.com", "acme", Role.MEMBER, projects=frozenset({"p1"}))


@pytest.fixture
def data(tmp_path, monkeypatch):
    async def populate() -> None:
        services = await build_services(Settings(data_dir=tmp_path / "data"))
        try:
            await services.knowledge.remember(
                ANN,
                RememberRequest(
                    title="Rollback runbook",
                    body="run make rollback pelican",
                    scope=Scope(ScopeLevel.PROJECT, "p1"),
                ),
            )
            await services.knowledge.remember(
                ANN, RememberRequest(title="Private idea", body="try the walrus approach")
            )
        finally:
            await services.close()

    asyncio.run(populate())
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def titles(out) -> list[str]:
    return [json.loads(x)["title"] for x in (out / "items.jsonl").read_text().splitlines()]


def test_export_leaves_private_notes_out_unless_asked_and_warns(data):
    out = data / "out"
    res = runner.invoke(cli, ["export", "--org", "acme", "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert "exported 1 items and 0 links" in res.output and "WARNING" not in res.output
    assert titles(out) == ["Rollback runbook"]
    assert stat.S_IMODE((out / "items.jsonl").stat().st_mode) == 0o600
    loud = runner.invoke(
        cli, ["export", "--org", "acme", "--out", str(data / "all"), "--include-private"]
    )
    assert loud.exit_code == 0 and "WARNING: this export contains PRIVATE notes" in loud.output
    assert sorted(titles(data / "all")) == ["Private idea", "Rollback runbook"]
    manifest = json.loads((data / "all" / "manifest.json").read_text())
    assert manifest["flags"]["include_private"] is True and manifest["counts"]["items"] == 2


def test_export_pseudonymises_and_reports_errors_cleanly(data):
    out = data / "p"
    res = runner.invoke(
        cli, ["export", "--org", "acme", "--out", str(out), "--pseudonymise", "--include-private"]
    )
    assert res.exit_code == 0, res.output
    assert "ann@acme.com" not in (out / "items.jsonl").read_text()
    again = runner.invoke(cli, ["export", "--org", "acme", "--out", str(out)])
    assert again.exit_code == 1 and "not an empty directory" in again.output
    bad = runner.invoke(cli, ["export", "--org", "acme", "--out", str(data / "b"), "--since", "x"])
    assert bad.exit_code == 1 and "--since" in bad.output
    fmt = runner.invoke(
        cli, ["export", "--org", "acme", "--out", str(data / "c"), "--format", "csv"]
    )
    assert fmt.exit_code == 1 and "jsonl or parquet" in fmt.output
    future = runner.invoke(
        cli, ["export", "--org", "acme", "--out", str(data / "d"), "--since", "2999-01-01"]
    )
    assert future.exit_code == 0 and "exported 0 items" in future.output
