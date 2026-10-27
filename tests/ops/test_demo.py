import asyncio

from typer.testing import CliRunner

from hive_mcp.cli import cli
from hive_mcp.cli_demo import DEMO, LABEL, SAMPLES, seed
from hive_mcp.config import Settings
from hive_mcp.security.tokens import TokenStore

runner = CliRunner()


def test_seeding_adds_notes_and_a_token_once(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    added, token = asyncio.run(seed(settings))
    assert added == len(SAMPLES) and token and token.startswith("hive_")
    assert [t.label for t in TokenStore(settings.tokens_path).list()] == [LABEL]
    again = asyncio.run(seed(settings))
    assert again == (0, None)  # nothing is duplicated, and a token is never shown twice


def test_the_demo_command_prints_how_to_connect_then_serves(tmp_path, monkeypatch):
    served = []
    monkeypatch.setattr("hive_mcp.cli_demo.run_server", served.append)
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    out = runner.invoke(cli, ["demo"]).output
    assert "5 example notes" in out and "claude mcp add --transport http hive" in out
    assert "Bearer hive_" in out and len(served) == 1
    second = runner.invoke(cli, ["demo"]).output
    assert "Bearer" not in second and "hive token revoke demo" in second


def test_the_demo_notes_are_searchable_by_the_demo_user(tmp_path):
    from hive_mcp.bootstrap import build_services
    from hive_mcp.services.models import ConsultRequest

    settings = Settings(data_dir=tmp_path / "data")
    asyncio.run(seed(settings))

    async def search() -> list[str]:
        services = await build_services(settings)
        try:
            found = await services.consult.consult(DEMO, ConsultRequest("release checklist"))
            return [m.title for m in found.primary]
        finally:
            await services.close()

    assert "Release checklist" in asyncio.run(search())
