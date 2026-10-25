from typer.testing import CliRunner

from hive_mcp.cli import cli

runner = CliRunner()


def test_ingest_directory_then_reingest_is_idempotent_and_reports_failures(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    (docs / "a.md").write_text("# Alpha\nradar pulse timing")
    (docs / "sub" / "b.txt").write_text("sonar ping echo")
    (docs / "bad.bin").write_bytes(b"\x00\x01")
    (docs / ".hidden").write_text("ignored")
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    args = ["ingest", str(docs), "--org", "o1", "--user", "u1", "--tag", "domain/test"]
    first = runner.invoke(cli, args)
    assert first.exit_code == 1  # one file failed, the others were still ingested
    assert "a.md: 1 parts, 1 new" in first.output and "b.txt: 1 parts, 1 new" in first.output
    assert "FAILED" in first.output and "bad.bin" in first.output and ".hidden" not in first.output
    second = runner.invoke(cli, args)
    assert "a.md: 1 parts, 0 new" in second.output and "b.txt: 1 parts, 0 new" in second.output


def test_reindex_queues_every_item(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    f = tmp_path / "n.txt"
    f.write_text("some knowledge")
    runner.invoke(cli, ["ingest", str(f), "--org", "o1", "--user", "u1"])
    out = runner.invoke(cli, ["reindex", "--org", "o1"])
    assert out.exit_code == 0 and "queued 1 items" in out.output
    assert "queued 0 items" in runner.invoke(cli, ["reindex", "--org", "other"]).output


def test_bad_role_is_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    f = tmp_path / "n.txt"
    f.write_text("x y")
    out = runner.invoke(cli, ["ingest", str(f), "--org", "o", "--user", "u", "--role", "god"])
    assert out.exit_code == 1 and "error" in out.output
