"""`hive-hooks init`: editor entries merged without a token, hooks, the agents note, a stored token."""

import json
import stat

import pytest
from hive_hooks import editors
from hive_hooks.install import HooksError
from typer.testing import CliRunner

from hive_mcp.cli import cli

runner = CliRunner()
URL = "https://kb.example.com/mcp"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HIVE_TOKEN", raising=False)
    monkeypatch.delenv("HIVE_URL", raising=False)
    from hive_hooks import connection

    monkeypatch.setattr(connection, "TOKEN_FILE", tmp_path / "home" / ".config" / "hive" / "token")
    return tmp_path / "home"


def test_each_editor_gets_the_format_it_reads_and_never_a_token():
    assert editors.merge("claude", {}, URL) == {
        "mcpServers": {
            "hive": {
                "type": "http",
                "url": URL,
                "headers": {"Authorization": "Bearer ${HIVE_TOKEN}"},
            }
        }
    }
    cursor = editors.merge("cursor", {}, URL)["mcpServers"]["hive"]
    assert cursor == {"url": URL, "headers": {"Authorization": "Bearer ${env:HIVE_TOKEN}"}}
    vscode = editors.merge("vscode", {}, URL)
    assert vscode["servers"]["hive"]["headers"]["Authorization"] == "Bearer ${input:hive-token}"
    assert vscode["inputs"] == [
        {"type": "promptString", "id": "hive-token", "password": True,
         "description": "Hive token (hive token create, on the server)"}
    ]  # fmt: skip


def test_merging_keeps_other_servers_is_repeatable_and_removable():
    other = {"mcpServers": {"git": {"command": "git-mcp"}}, "extra": 1}
    once = editors.merge("claude", other, URL)
    assert once["mcpServers"]["git"] == {"command": "git-mcp"} and once["extra"] == 1
    assert editors.merge("claude", once, URL) == once
    moved = editors.merge("claude", once, "https://other/mcp")
    assert moved["mcpServers"]["hive"]["url"] == "https://other/mcp"
    assert editors.remove("claude", once) == other
    vs = editors.merge("vscode", {"inputs": [{"id": "keep"}]}, URL)
    assert editors.merge("vscode", vs, URL) == vs  # the prompt input is not duplicated
    assert editors.remove("vscode", vs) == {"inputs": [{"id": "keep"}]}
    assert editors.remove("vscode", editors.merge("vscode", {}, URL)) == {}


def test_a_file_with_the_wrong_shape_or_an_unknown_editor_is_refused():
    with pytest.raises(HooksError, match="not an object"):
        editors.merge("claude", {"mcpServers": []}, URL)
    for call in (lambda: editors.entry_for("emacs", URL), lambda: editors.path_for("emacs", ".")):
        with pytest.raises(HooksError, match="editor must be one of"):
            call()


def test_init_writes_hooks_editor_files_and_the_note_in_one_go(tmp_path, home):
    project = tmp_path / "proj"
    project.mkdir()
    result = runner.invoke(
        cli,
        ["init", "--url", URL, "--editor", "all", "--root", str(project),
         "--agents-file", str(project / "AGENTS.md"), "--no-check"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "hive hook session-end" in (home / ".claude" / "settings.json").read_text()
    for name in (".mcp.json", ".cursor/mcp.json", ".vscode/mcp.json"):
        assert URL in (project / name).read_text()
        assert "hive_" not in (project / name).read_text()
    assert "hive-mcp:begin" in (project / "AGENTS.md").read_text()
    again = runner.invoke(
        cli,
        ["init", "--url", URL, "--editor", "claude", "--root", str(project), "--no-check"],
    )
    assert again.exit_code == 0 and json.loads((project / ".mcp.json").read_text())["mcpServers"]


def test_init_asks_for_the_address_and_names_a_bad_editor(tmp_path):
    no_url = runner.invoke(cli, ["init", "--no-check"])
    assert no_url.exit_code == 1 and "--url" in no_url.output
    bad = runner.invoke(cli, ["init", "--url", URL, "--editor", "emacs", "--no-check"])
    assert bad.exit_code == 1 and "emacs" in bad.output


def test_init_reads_the_address_from_the_environment_and_stores_a_token_owner_only(
    tmp_path, home, monkeypatch
):
    monkeypatch.setenv("HIVE_URL", URL)
    monkeypatch.setenv("HIVE_TOKEN", "hive_secret")
    result = runner.invoke(
        cli, ["init", "--save-token", "--no-hooks", "--root", str(tmp_path), "--no-check"]
    )
    assert result.exit_code == 0, result.output
    token = home / ".config" / "hive" / "token"
    assert token.read_text().strip() == "hive_secret"
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert (
        "hive_secret" not in result.output
        and "hive_secret" not in (tmp_path / ".mcp.json").read_text()
    )
    assert not (home / ".claude").exists()  # --no-hooks


def test_init_without_a_token_to_save_says_so(tmp_path):
    result = runner.invoke(
        cli, ["init", "--url", URL, "--save-token", "--root", str(tmp_path), "--no-check"]
    )
    assert result.exit_code == 1 and "HIVE_TOKEN" in result.output
