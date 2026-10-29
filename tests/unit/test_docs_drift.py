"""The documentation must not drift from the code: links resolve, tools / commands / settings it names exist,
and every package of the source tree is described in the architecture document."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer.main

from hive_mcp.cli import cli
from hive_mcp.config import Settings

ROOT = Path(__file__).parents[2]
if not (ROOT / "README.md").is_file():  # the mutation-testing copy of the tree has no docs
    pytest.skip("documentation not present", allow_module_level=True)
DOCS = sorted([ROOT / "README.md", *(ROOT / "docs").glob("*.md")])
TEXT = {p: p.read_text() for p in DOCS}
# Generated or historical material that may mention things that no longer exist.
HISTORICAL = {"PLAN.md", "BENCHMARK.md"}
CHECKED = [p for p in DOCS if p.name not in HISTORICAL]


def tool_names() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "src" / "hive_mcp" / "mcp_api").glob("*.py"):
        found |= set(re.findall(r"async def (kb_\w+)\(", path.read_text()))
    return found


def commands() -> dict[str, set[str]]:
    root = typer.main.get_command(cli)
    out: dict[str, set[str]] = {}
    for name, command in root.commands.items():  # type: ignore[attr-defined]
        sub = getattr(command, "commands", None)
        out[name] = set(sub) if sub else set()
    return out


def known_settings() -> set[str]:
    names = {f"HIVE_{field.upper()}" for field in Settings.model_fields}
    return names


def repo_text() -> str:
    parts = []
    for folder in ("src", "scripts", "deploy", "tests", "clients", ".github"):
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and path.suffix in {
                ".py",
                ".sh",
                ".bicep",
                ".bicepparam",
                ".yml",
                ".yaml",
                ".toml",
            }:
                parts.append(path.read_text(errors="ignore"))
    parts += [
        (ROOT / n).read_text()
        for n in (".env.example", "Dockerfile", "pyproject.toml", "docker-compose.yml")
    ]
    return "\n".join(parts)


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_every_relative_link_resolves(path: Path) -> None:
    for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", TEXT[path]):
        if re.match(r"[a-z]+://|mailto:", target):
            continue
        assert (path.parent / target).resolve().exists(), f"{path.name} links to missing {target}"


@pytest.mark.parametrize("path", CHECKED, ids=lambda p: p.name)
def test_every_kb_tool_named_in_the_docs_exists(path: Path) -> None:
    names = set(re.findall(r"\b(kb_[a-z_]+)\b", TEXT[path]))
    known = tool_names()
    missing = sorted(n for n in names if n not in known and not n.endswith("_") and n != "kb_")
    assert not missing, f"{path.name} names tools that do not exist: {missing}"


@pytest.mark.parametrize("path", CHECKED, ids=lambda p: p.name)
def test_every_hive_command_in_the_docs_exists(path: Path) -> None:
    known = commands()
    shown = re.findall(
        r"^\s*(?:\$ )?(?:uv run )?hive ([a-z][a-z-]+)(?: ([a-z][a-z-]+))?", TEXT[path], re.M
    )
    for first, second in shown:
        assert first in known, f"{path.name}: `hive {first}` is not a command"
        if known[first] and second:  # a group: the next word must be one of its commands
            assert second in known[first], f"{path.name}: `hive {first} {second}` is not a command"


@pytest.mark.parametrize("path", CHECKED, ids=lambda p: p.name)
def test_every_setting_named_in_the_docs_exists_somewhere(path: Path) -> None:
    names = set(re.findall(r"\bHIVE_[A-Z][A-Z0-9_]+\b", TEXT[path]))
    known, text = known_settings(), repo_text()
    missing = sorted(n for n in names if n not in known and n not in text and not n.endswith("_"))
    assert not missing, f"{path.name} names settings that nothing reads: {missing}"


def test_the_architecture_document_mentions_every_source_package() -> None:
    architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text()
    packages = [
        p.name
        for p in (ROOT / "src" / "hive_mcp").iterdir()
        if p.is_dir() and not p.name.startswith(("_", "."))
    ]
    missing = [p for p in packages if f"{p}/" not in architecture and f"`{p}" not in architecture]
    assert not missing, f"ARCHITECTURE.md does not describe: {missing}"


def test_the_requirements_documents_exist_and_are_linked_from_the_readme() -> None:
    readme = (ROOT / "README.md").read_text()
    for name in ("REQUIREMENTS.md", "REQUIREMENTS-ANNEX.md", "ARCHITECTURE.md", "USAGE.md"):
        assert (ROOT / "docs" / name).is_file() and f"docs/{name}" in readme


ANNEX = (ROOT / "docs" / "REQUIREMENTS-ANNEX.md").read_text() if (ROOT / "docs").is_dir() else ""


def test_the_annex_lists_every_tool_and_setting_and_command_the_code_has() -> None:
    missing_tools = sorted(t for t in tool_names() if f"`{t}`" not in ANNEX and t not in ANNEX)
    missing_settings = sorted(s for s in known_settings() if s not in ANNEX)
    root = typer.main.get_command(cli)
    missing_commands = sorted(
        f"hive {name}"
        for name in root.commands
        if f"hive {name}" not in ANNEX  # type: ignore[attr-defined]
    )
    assert not (missing_tools or missing_settings or missing_commands), (
        f"the annex lacks tools {missing_tools}, settings {missing_settings}, "
        f"commands {missing_commands}"
    )
