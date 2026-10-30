import asyncio

from typer.testing import CliRunner

from hive_mcp.bootstrap import build_services
from hive_mcp.cli import cli
from hive_mcp.config import Settings

runner = CliRunner()


def drain():
    async def go():
        services = await build_services(Settings())
        try:
            while await services.runner.run_once():
                pass
        finally:
            await services.close()

    asyncio.run(go())


def run_id_of(output):
    return output.split("queued bulk ingest ")[1].split()[0]


def test_background_ingest_status_and_cancel(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(3):
        (docs / f"n{i}.md").write_text(f"# Note {i}\nsome words {i}")
    (docs / "bad.bin").write_bytes(b"\x00")
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    args = ["ingest", str(docs), "--org", "o1", "--user", "u1", "--background"]
    out = runner.invoke(cli, args)
    assert out.exit_code == 0 and "must exist inside the server's container" in out.output
    first = run_id_of(out.output)
    listing = runner.invoke(cli, ["ingest-status", "--org", "o1"])
    assert first in listing.output and "running" in listing.output
    drain()
    one = runner.invoke(cli, ["ingest-status", first])
    assert "done" in one.output and "3 done, 1 failed, 0 skipped" in one.output
    assert "bad.bin" in one.output  # the reason is listed for a single run
    second = run_id_of(runner.invoke(cli, args).output)
    assert runner.invoke(cli, ["ingest-cancel", second]).exit_code == 0
    drain()
    assert "cancelled" in runner.invoke(cli, ["ingest-status", second]).output
    assert runner.invoke(cli, ["ingest-cancel", second]).exit_code == 1  # already finished
    assert runner.invoke(cli, ["ingest-status", "nope"]).output.startswith("no such")
    assert runner.invoke(cli, ["ingest-status"]).exit_code == 1  # needs an id or --org


def test_background_refuses_bad_folders_and_paths_outside_the_root(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    (allowed / "in").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    (other / "f.txt").write_text("x y")
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HIVE_INGEST_ROOT", str(allowed))
    base = ["--org", "o1", "--user", "u1", "--background"]
    outside = runner.invoke(cli, ["ingest", str(other), *base])
    assert outside.exit_code == 1 and "outside the allowed ingest root" in outside.output
    file_ = runner.invoke(cli, ["ingest", str(other / "f.txt"), *base])
    assert file_.exit_code == 1 and "exactly one folder" in file_.output
    two = runner.invoke(cli, ["ingest", str(allowed), str(other), *base])
    assert two.exit_code == 1
    assert runner.invoke(cli, ["ingest", str(allowed / "in"), *base]).exit_code == 0
