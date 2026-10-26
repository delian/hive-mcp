"""Putting the hooks into Claude Code's settings.json (and a note into AGENTS.md / CLAUDE.md).

Changes are merged, never replaced: other hooks and settings are left alone, running it twice changes
nothing, and a settings file that cannot be parsed is refused rather than overwritten.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


class HooksError(Exception):
    """A problem the user can fix; the message says how."""


MARKER = " hook "  # our commands look like "hive hook <event>"
BEGIN = "<!-- hive-mcp:begin -->"
END = "<!-- hive-mcp:end -->"
# (settings event, our event name, timeout seconds)
EVENTS = (
    ("SessionStart", "session-start", 10),
    ("UserPromptSubmit", "prompt", 8),
    ("PreCompact", "pre-compact", 30),
    ("SessionEnd", "session-end", 30),
)
SNIPPET = f"""{BEGIN}
## Team knowledge base (hive-mcp)
A shared knowledge base is available through the `hive` MCP server.
- Before starting a task, call `kb_consult` with the task in plain words and use what it returns (say where it came from;
  results marked EXTERNAL belong to groups the user is not in).
- When you finish something reusable (a finding, formula, measurement, decision, how-to), call `kb_remember`: one idea per
  item, a clear title, tags such as `domain/rf`. Never store secrets or personal data.
- At the end of a session call `kb_report_session`; before the context is compacted call `kb_report_compaction`.
{END}
"""


def default_command() -> str:
    """How Claude Code should start us: the name this program was started with (hive or hive-hooks)."""
    name = Path(sys.argv[0]).name
    return name if name in {"hive", "hive-hooks"} else "hive"


def is_ours(entry: Any) -> bool:  # noqa: ANN401 - settings.json content
    hooks = entry.get("hooks") if isinstance(entry, dict) else None
    return isinstance(hooks, list) and any(
        isinstance(h, dict) and MARKER in str(h.get("command", "")) and "hive" in str(h["command"])
        for h in hooks
    )


def _clean(settings: dict[str, Any]) -> dict[str, Any]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for event, entries in list(hooks.items()):
        if isinstance(entries, list):
            kept = [e for e in entries if not is_ours(e)]
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    if not hooks:
        del settings["hooks"]
    return settings


def merge(
    settings: dict[str, Any], *, command: str = "hive", prompt_hook: bool = True
) -> dict[str, Any]:
    """`settings` with our hooks added (after removing any older copy of them)."""
    result = _clean(json.loads(json.dumps(settings)))
    hooks = result.setdefault("hooks", {})
    for event, name, timeout in EVENTS:
        if name == "prompt" and not prompt_hook:
            continue
        entry = {
            "hooks": [{"type": "command", "command": f"{command} hook {name}", "timeout": timeout}]
        }
        hooks.setdefault(event, []).append(entry)
    return result


def remove(settings: dict[str, Any]) -> dict[str, Any]:
    return _clean(json.loads(json.dumps(settings)))


def installed_events(settings: dict[str, Any]) -> list[str]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    return sorted(
        e
        for e, entries in hooks.items()
        if isinstance(entries, list) and any(map(is_ours, entries))
    )


def settings_path(scope: str, cwd: Path | None = None) -> Path:
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    if scope == "project":
        return (cwd or Path.cwd()) / ".claude" / "settings.json"
    if scope == "local":
        return (cwd or Path.cwd()) / ".claude" / "settings.local.json"
    raise HooksError("scope must be user, project or local")


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError as exc:
        raise HooksError(f"{path} is not valid JSON; fix it first (nothing was changed)") from exc
    if not isinstance(data, dict):
        raise HooksError(f"{path} does not hold a JSON object; nothing was changed")
    return data


def save(path: Path, settings: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".hive-backup")
    if path.exists() and not backup.exists():
        backup.write_bytes(path.read_bytes())  # the original, kept once
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(settings, handle, indent=2)
            handle.write("\n")
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def apply_snippet(text: str) -> str:
    """`text` with our note between its markers (added at the end when absent)."""
    start, end = text.find(BEGIN), text.find(END)
    if start != -1 and end > start:
        return text[:start] + SNIPPET.rstrip("\n") + text[end + len(END) :]
    separator = (
        "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    )
    return text + separator + SNIPPET


def strip_snippet(text: str) -> str:
    start, end = text.find(BEGIN), text.find(END)
    if start == -1 or end < start:
        return text
    return (text[:start].rstrip("\n") + "\n" + text[end + len(END) :].lstrip("\n")).lstrip("\n")
