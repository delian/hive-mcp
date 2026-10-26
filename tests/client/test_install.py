"""Installing the hooks: merged into existing settings, idempotent, reversible, never destructive."""

import json

import pytest
from hive_hooks import install
from hive_hooks.install import HooksError
from typer.testing import CliRunner

from hive_mcp.cli import cli

runner = CliRunner()
OTHER = {"matcher": "Bash", "hooks": [{"type": "command", "command": "./audit.sh"}]}


def test_merge_adds_our_hooks_next_to_existing_ones_and_keeps_other_settings():
    before = {"model": "opus", "hooks": {"PreToolUse": [OTHER], "SessionStart": [OTHER]}}
    after = install.merge(before)
    assert after["model"] == "opus" and after["hooks"]["PreToolUse"] == [OTHER]
    assert after["hooks"]["SessionStart"][0] == OTHER and len(after["hooks"]["SessionStart"]) == 2
    commands = {
        h["command"] for entries in after["hooks"].values() for e in entries for h in e["hooks"]
    }
    assert {
        "hive hook session-start",
        "hive hook prompt",
        "hive hook pre-compact",
        "hive hook session-end",
    } <= commands
    assert before["hooks"]["SessionStart"] == [OTHER]  # the input is not modified


def test_merging_twice_changes_nothing_and_a_changed_command_replaces_the_old_one():
    once = install.merge({})
    assert install.merge(once) == once
    moved = install.merge(once, command="uvx --from hive-mcp hive")
    cmds = [h["command"] for e in moved["hooks"]["SessionStart"] for h in e["hooks"]]
    assert cmds == ["uvx --from hive-mcp hive hook session-start"]


def test_the_prompt_hook_is_optional():
    assert "UserPromptSubmit" not in install.merge({}, prompt_hook=False)["hooks"]
    assert "UserPromptSubmit" not in install.merge(install.merge({}), prompt_hook=False)["hooks"]


def test_remove_deletes_only_ours_and_cleans_up_empty_sections():
    settings = {"hooks": {"SessionStart": [OTHER]}, "model": "x"}
    assert install.remove(install.merge(settings)) == settings
    assert install.remove(install.merge({})) == {}
    assert install.installed_events(install.merge({})) == [
        "PreCompact",
        "SessionEnd",
        "SessionStart",
        "UserPromptSubmit",
    ]
    assert install.installed_events(settings) == []


def test_other_peoples_hooks_that_merely_mention_hive_are_not_ours():
    lookalike = {"hooks": [{"type": "command", "command": "echo hive"}]}
    assert (
        not install.is_ours(lookalike)
        and not install.is_ours("junk")
        and not install.is_ours({"hooks": "x"})
    )
    assert install.remove({"hooks": {"Stop": [lookalike]}}) == {"hooks": {"Stop": [lookalike]}}


def test_files_are_written_atomically_with_the_original_backed_up_once(tmp_path):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"model": "keep"}))
    install.save(path, install.merge(install.load(path)))
    install.save(path, install.merge(install.load(path)))
    backup = path.with_name("settings.json.hive-backup")
    assert json.loads(backup.read_text()) == {"model": "keep"}  # the first, untouched original
    assert json.loads(path.read_text())["model"] == "keep" and "hooks" in json.loads(
        path.read_text()
    )
    assert not [
        p
        for p in path.parent.iterdir()
        if p.name.startswith("settings.json") and p.suffix == ".tmp"
    ]


def test_a_broken_settings_file_is_refused_not_overwritten(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    with pytest.raises(HooksError, match="not valid JSON"):
        install.load(path)
    path.write_text("[1]")
    with pytest.raises(HooksError, match="JSON object"):
        install.load(path)
    assert path.read_text() == "[1]"


def test_scopes():
    assert str(install.settings_path("user")).endswith(".claude/settings.json")
    assert (
        install.settings_path("project", tmp := __import__("pathlib").Path("/x"))
        == tmp / ".claude/settings.json"
    )
    assert install.settings_path("local", tmp).name == "settings.local.json"
    with pytest.raises(HooksError):
        install.settings_path("galaxy")


# ---------------------------------------------------------------- the note for AGENTS.md
def test_the_note_is_added_updated_and_removed_in_place():
    text = "# Rules\nBe nice.\n"
    added = install.apply_snippet(text)
    assert added.startswith(text) and install.BEGIN in added and "kb_consult" in added
    assert install.apply_snippet(added) == added  # idempotent
    edited = added.replace("Be nice.", "Be kind.")
    assert install.apply_snippet(edited).count(
        install.BEGIN
    ) == 1 and "Be kind." in install.apply_snippet(edited)
    assert install.strip_snippet(added) == text
    assert install.strip_snippet(text) == text and install.apply_snippet("").startswith(
        install.BEGIN
    )


def test_install_command_end_to_end(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = runner.invoke(cli, ["hooks", "install", "--scope", "project", "--dry-run"])
    assert out.exit_code == 0 and "hive hook session-start" in out.output
    assert not (tmp_path / ".claude").exists()  # a dry run writes nothing
    out = runner.invoke(cli, ["hooks", "install", "--scope", "project", "--no-prompt-hook"])
    assert out.exit_code == 0 and "HIVE_TOKEN" in out.output
    saved = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert set(saved["hooks"]) == {"SessionStart", "PreCompact", "SessionEnd"}
    assert "removed" in runner.invoke(cli, ["hooks", "remove", "--scope", "project"]).output
    assert "no hive hooks" in runner.invoke(cli, ["hooks", "remove", "--scope", "project"]).output
    (tmp_path / ".claude" / "settings.json").write_text("{oops")
    bad = runner.invoke(cli, ["hooks", "install", "--scope", "project"])
    assert bad.exit_code == 1 and "not valid JSON" in bad.output
    note = tmp_path / "AGENTS.md"
    runner.invoke(cli, ["hooks", "snippet", "--file", str(note)])
    assert install.BEGIN in note.read_text()
    runner.invoke(cli, ["hooks", "snippet", "--file", str(note), "--remove"])
    assert install.BEGIN not in note.read_text()


def test_the_default_command_is_the_program_that_was_started(monkeypatch):
    for argv0, expected in (
        ("/usr/local/bin/hive-hooks", "hive-hooks"), ("/x/hive", "hive"),
        ("/x/pytest", "hive"), ("python -m foo", "hive"),
    ):  # fmt: skip
        monkeypatch.setattr("sys.argv", [argv0])
        assert install.default_command() == expected
