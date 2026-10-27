"""MCP server entries for editors and agents, written so that no token ever lands in a file.

Project files are usually committed, so the token is referenced the way each tool expects (an
environment variable, or a prompt in VS Code) and the entry is merged into what is already there.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from hive_hooks.install import HooksError

if TYPE_CHECKING:
    from pathlib import Path

NAME = "hive"
EDITORS = ("claude", "vscode", "cursor")


def _http(url: str, *, auth: str, kind: str | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"url": url, "headers": {"Authorization": auth}}
    return {"type": kind, **entry} if kind else entry


def entry_for(editor: str, url: str) -> tuple[str, dict[str, Any]]:
    """(top-level key, the server entry) for an editor's MCP file."""
    if editor == "claude":  # .mcp.json: ${VAR} is expanded when Claude Code starts
        return "mcpServers", _http(url, auth="Bearer ${HIVE_TOKEN}", kind="http")
    if editor == "cursor":  # .cursor/mcp.json
        return "mcpServers", _http(url, auth="Bearer ${env:HIVE_TOKEN}", kind=None)
    if editor == "vscode":  # .vscode/mcp.json: asks once and keeps the answer outside the file
        return "servers", _http(url, auth="Bearer ${input:hive-token}", kind="http")
    raise HooksError(f"editor must be one of {', '.join(EDITORS)}")


def path_for(editor: str, root: Path) -> Path:
    files = {"claude": ".mcp.json", "cursor": ".cursor/mcp.json", "vscode": ".vscode/mcp.json"}
    if editor not in files:
        raise HooksError(f"editor must be one of {', '.join(EDITORS)}")
    return root / files[editor]


def merge(editor: str, existing: dict[str, Any], url: str) -> dict[str, Any]:
    """`existing` with the hive server added or updated; every other server stays."""
    key, entry = entry_for(editor, url)
    result: dict[str, Any] = json.loads(json.dumps(existing))
    servers = result.setdefault(key, {})
    if not isinstance(servers, dict):
        raise HooksError(f'"{key}" in the file is not an object; nothing was changed')
    servers[NAME] = entry
    if editor == "vscode":
        inputs = [i for i in result.get("inputs", []) if i.get("id") != "hive-token"]
        inputs.append(
            {
                "type": "promptString", "id": "hive-token", "password": True,
                "description": "Hive token (hive token create, on the server)",
            }
        )  # fmt: skip
        result["inputs"] = inputs
    return result


def remove(editor: str, existing: dict[str, Any]) -> dict[str, Any]:
    key, _ = entry_for(editor, "https://x")
    result: dict[str, Any] = json.loads(json.dumps(existing))
    servers = result.get(key)
    if isinstance(servers, dict):
        servers.pop(NAME, None)
        if not servers:
            del result[key]
    if editor == "vscode":
        kept = [i for i in result.get("inputs", []) if i.get("id") != "hive-token"]
        if kept:
            result["inputs"] = kept
        else:
            result.pop("inputs", None)
    return result
