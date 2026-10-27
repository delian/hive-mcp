import asyncio
import json

import pytest
from typer.testing import CliRunner

from hive_mcp.cli import cli
from hive_mcp.config import Settings
from hive_mcp.connectors.base import SyncReport
from hive_mcp.connectors.spec import parse_specs
from hive_mcp.ops import loops

runner = CliRunner()
RAW = [{"name": "wiki", "type": "urls", "urls": ["https://x/y"], "org": "o", "owner": "bot",
        "project": "p1", "every_minutes": 5}]  # fmt: skip


class KV:
    def __init__(self):
        self.claims = set()

    async def put_if_absent(self, ns, key, value, *, ttl_seconds=None):
        if (ns, key) in self.claims:
            return False
        self.claims.add((ns, key))
        return True


async def test_each_connector_runs_once_per_slot_even_with_two_replicas(monkeypatch):
    kv, ran = KV(), []

    class Sync:
        async def run(self, spec, connector, **_):
            ran.append(spec.name)
            return SyncReport()

    class Services:
        connectors = Sync()

        class backend:
            pass

    Services.backend.kv = kv
    monkeypatch.setattr(
        "hive_mcp.connectors.factory.make_connector", lambda spec, settings: object()
    )
    monkeypatch.setattr(loops, "CONNECTOR_CHECK_SECONDS", 0.01)
    specs = parse_specs(RAW)
    tasks = [
        asyncio.create_task(loops.connector_loop(Services, Settings(), specs)) for _ in range(2)
    ]
    await asyncio.sleep(0.2)
    for t in tasks:
        t.cancel()
    assert ran == ["wiki"]  # two replicas, many checks, one run in this slot


def test_the_cli_lists_and_explains_missing_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    assert "no connectors" in runner.invoke(cli, ["connectors", "list"]).output
    assert "no connectors configured" in runner.invoke(cli, ["connectors", "run"]).output
    monkeypatch.setenv("HIVE_CONNECTORS", json.dumps(RAW))
    out = runner.invoke(cli, ["connectors", "list"]).output
    assert "wiki\turls\tevery 5 min\tinto project p1" in out
    assert "no such connector" in runner.invoke(cli, ["connectors", "run", "nope"]).output
    assert "never ran" in runner.invoke(cli, ["connectors", "status"]).output


def test_bad_connector_settings_stop_the_server_at_startup():
    with pytest.raises(ValueError, match="HIVE_CONNECTORS"):
        Settings(connectors=[{"name": "x", "type": "urls"}])
    assert Settings(connectors=RAW).connectors


async def test_unreadable_secrets_are_reported_by_name_not_value(monkeypatch, tmp_path):
    from hive_mcp.connectors.factory import make_connector
    from hive_mcp.domain.errors import ValidationError

    gh = parse_specs([{"name": "g", "type": "github", "repo": "a/b", "token_env": "NOPE_TOKEN",
                       "org": "o", "owner": "b", "project": "p"}])[0]  # fmt: skip
    monkeypatch.delenv("NOPE_TOKEN", raising=False)
    with pytest.raises(ValidationError, match="NOPE_TOKEN is not set"):
        make_connector(gh, Settings())
    monkeypatch.setenv("NOPE_TOKEN", "ghp_x")
    assert make_connector(gh, Settings())._http.headers["authorization"] == "Bearer ghp_x"
    dr = parse_specs([{"name": "d", "type": "drive", "folder": "f", "key_file": str(tmp_path / "no.json"),
                       "as_user": "a@b.c", "org": "o", "owner": "b", "project": "p"}])[0]  # fmt: skip
    with pytest.raises(ValidationError, match="cannot read the key file"):
        make_connector(dr, Settings())
