"""Reading a Claude Code transcript (JSONL) defensively: its shape is not a stable contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

MAX_READ_BYTES = 4 * 1024 * 1024  # the tail of very long sessions is what matters
MAX_LOG_CHARS = 60_000
MAX_SUMMARY_CHARS = 1_500


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


def read_turns(path: str | Path) -> list[Turn]:
    """User and assistant text only (no tool calls or results), oldest first."""
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(size - MAX_READ_BYTES, 0))
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    turns = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # a partial first line after seeking
        message = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(message, dict) or entry.get("isSidechain"):
            continue
        role = message.get("role")
        text = _text(message.get("content")).strip()
        skip = text.startswith(("<command-", "<local-command", "<system-reminder"))
        if role in {"user", "assistant"} and text and not skip:
            turns.append(Turn(role, text))
    return turns


def render_log(turns: list[Turn], limit: int = MAX_LOG_CHARS) -> str:
    """The conversation as text, keeping the END when it is too long."""
    text = "\n\n".join(f"[{t.role}] {t.text}" for t in turns)
    return text if len(text) <= limit else "[...earlier turns omitted...]\n" + text[-limit:]


def mechanical_summary(turns: list[Turn], project: str, reason: str = "") -> str:
    """No model is involved: the first request, the last answer and the size of the session."""
    users = [t for t in turns if t.role == "user"]
    assistants = [t for t in turns if t.role == "assistant"]
    parts = [f"Session in {project}: {len(users)} requests, {len(assistants)} replies."]
    if reason:
        parts[0] = parts[0][:-1] + f" (ended: {reason})."
    if users:
        parts.append(f"First request: {users[0].text[:600]}")
    if assistants:
        parts.append(f"Last reply: {assistants[-1].text[:600]}")
    return "\n".join(parts)[:MAX_SUMMARY_CHARS]
