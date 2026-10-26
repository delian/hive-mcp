"""Reading a Claude Code transcript (JSONL) defensively: its shape is not a stable contract."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

MAX_READ_BYTES = 4 * 1024 * 1024  # the tail of very long sessions is what matters
MAX_LOG_CHARS = 60_000


@dataclass(frozen=True, slots=True)
class Turn:
    role: str  # "user" or "assistant"
    text: str


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in parts if isinstance(p, str))
    return ""


def _entries(path: str | Path) -> Iterator[dict[str, Any]]:
    """The transcript's JSON objects (main conversation only), skipping lines we cannot read."""
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(size - MAX_READ_BYTES, 0))
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # a partial first line after seeking
        if isinstance(entry, dict) and not entry.get("isSidechain"):
            yield entry


def read_turns(path: str | Path) -> list[Turn]:
    """User and assistant text only (no tool calls or results), oldest first."""
    turns = []
    for entry in _entries(path):
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        text = _text(message.get("content")).strip()
        skip = text.startswith(("<command-", "<local-command", "<system-reminder"))
        if role in {"user", "assistant"} and text and not skip:
            turns.append(Turn(role, text))
    return turns


@dataclass(frozen=True, slots=True)
class Activity:
    files: tuple[str, ...]  # files the agent edited or wrote, in first-touched order
    commits: tuple[str, ...]  # messages of `git commit -m ...` commands it ran
    commands: int  # how many shell commands it ran


EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_COMMIT = re.compile(r"git\s+commit\b[^\n]*?\s-[a-zA-Z]*m\s+(?:\"([^\"]+)\"|'([^']+)')")
MAX_FILES = 12


def read_activity(path: str | Path) -> Activity:
    """What the agent did to the world, from the tool calls in the transcript."""
    files: dict[str, None] = {}
    commits: list[str] = []
    commands = 0
    for entry in _entries(path):
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            raw_input = block.get("input")
            args: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
            if block.get("name") in EDIT_TOOLS:
                target = args.get("file_path") or args.get("notebook_path")
                if isinstance(target, str):
                    files.setdefault(target, None)
            elif block.get("name") == "Bash" and isinstance(args.get("command"), str):
                commands += 1
                for match in _COMMIT.finditer(args["command"]):
                    commits.append((match.group(1) or match.group(2)).strip().splitlines()[0])
    return Activity(tuple(files), tuple(commits), commands)


def render_log(turns: list[Turn], limit: int = MAX_LOG_CHARS) -> str:
    """The conversation as text, keeping the END when it is too long."""
    text = "\n\n".join(f"[{t.role}] {t.text}" for t in turns)
    return text if len(text) <= limit else "[...earlier turns omitted...]\n" + text[-limit:]
