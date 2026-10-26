import shutil
import subprocess

import pytest
from typer.testing import CliRunner

from hive_mcp.cli import cli
from hive_mcp.domain.errors import ValidationError
from hive_mcp.ingest.git import clone, repo_name, select_files

runner = CliRunner()
pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def git(cwd, *args):
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x.io",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x.io",
        "PATH": __import__("os").environ["PATH"],
        "HOME": str(cwd),
    }
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)  # noqa: S603, S607


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "pulse-tools"
    (root / "docs").mkdir(parents=True)
    (root / "node_modules").mkdir()
    (root / "README.md").write_text("# Pulse tools\nHelpers for radar pulse timing.")
    (root / "docs" / "guide.md").write_text("# Guide\nHow to calibrate the receiver chain.")
    (root / "main.py").write_text('"""Entry point."""\nprint("hello")\n')
    (root / "logo.png").write_bytes(b"\x89PNG\r\n")
    (root / "node_modules" / "dep.js").write_text("module.exports = 1")
    (root / "huge.txt").write_text("x " * 600_000)
    (root / "notes.lock").write_text("lockfile")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def test_repo_names():
    assert repo_name("https://github.com/acme/pulse-tools.git") == "pulse-tools"
    assert repo_name("git@github.com:acme/pulse-tools.git") == "pulse-tools"
    assert repo_name("/srv/git/radar/") == "radar"


def test_clone_and_select_skips_binaries_generated_code_lockfiles_and_big_files(repo, tmp_path):
    dest = tmp_path / "clone"
    clone(f"file://{repo}", dest)
    files, skipped = select_files(dest)
    assert sorted(f.relative_to(dest).as_posix() for f in files) == [
        "README.md",
        "docs/guide.md",
        "main.py",
    ]
    assert (
        skipped == 4
    )  # png, node_modules/dep.js, huge.txt, notes.lock (the .git directory is hidden, not skipped)


def test_clone_refuses_dangerous_or_broken_addresses(tmp_path):
    for bad in ("--upload-pack=touch /tmp/pwned", "-c", "", "https://example.invalid/a\nb"):
        with pytest.raises(ValidationError, match="not a usable"):
            clone(bad, tmp_path / "x")
    with pytest.raises(ValidationError, match="git clone failed"):
        clone(f"file://{tmp_path}/no-such-repo", tmp_path / "y")
    assert not (tmp_path / "pwned").exists()


def test_cli_ingests_a_repo_idempotently_with_repo_tag_and_source(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    args = ["ingest-git", f"file://{repo}", "--org", "o1", "--user", "u1", "--tag", "team/radar"]
    first = runner.invoke(cli, args)
    assert first.exit_code == 0, first.output
    assert (
        "3 files to read, 4 skipped" in first.output and "README.md: 1 parts, 1 new" in first.output
    )
    again = runner.invoke(cli, args)
    assert (
        "README.md: 1 parts, 0 new" in again.output
        and "docs/guide.md: 1 parts, 0 new" in again.output
    )

    from hive_mcp.adapters.sqlite import SqliteBackend  # noqa: PLC0415
    from hive_mcp.domain.item import ItemQuery  # noqa: PLC0415
    from hive_mcp.domain.principal import Principal  # noqa: PLC0415

    backend = SqliteBackend(tmp_path / "data")
    try:
        import asyncio  # noqa: PLC0415

        page = asyncio.run(backend.items.list(Principal("u1", "o1"), ItemQuery(limit=10)))
    finally:
        backend.close()
    assert len(page.items) == 3
    guide = next(
        v.item for v in page.items if "guide" in v.item.title.lower() or "Guide" in v.item.title
    )
    assert {"team/radar", "repo/pulse-tools"} <= guide.tags
    assert guide.source == f"git:file://{repo}#docs/guide.md"


def test_cli_reports_clone_failures_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    out = runner.invoke(
        cli, ["ingest-git", f"file://{tmp_path}/missing", "--org", "o", "--user", "u"]
    )
    assert out.exit_code == 1 and "git clone failed" in out.output
