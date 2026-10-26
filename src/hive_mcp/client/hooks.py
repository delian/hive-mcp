"""What each Claude Code hook does. Handlers return the text for the agent (or None) and never
raise: a knowledge base that is down must not get in the way of someone's work."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from hive_mcp.client.connection import call_tool
from hive_mcp.client.summary import outcomes, summarize
from hive_mcp.client.transcript import read_activity, read_turns, render_log

log = logging.getLogger("hive.hook")
MIN_PROMPT_WORDS = 6
MAX_PROMPT_CHARS = 500
MAX_CONTEXT_CHARS = 3000


def project_name(cwd: str) -> str:
    return Path(cwd or os.getcwd()).name or "project"


def _context(event: str, text: str) -> str:
    """Claude Code's JSON form for adding context; the event name is required."""
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}})


def format_results(results: list[dict[str, Any]], heading: str) -> str:
    lines = [heading]
    for r in results:
        origin = r.get("origin") or ""
        snippet = " ".join(str(r.get("snippet") or "").split())[:240]
        lines.append(f"- {r.get('title')} [{r.get('id')}] ({origin}): {snippet}")
    lines.append("Open an item in full with kb_get; cite its origin when you use it.")
    return "\n".join(lines)[:MAX_CONTEXT_CHARS]


async def session_start(payload: dict[str, Any]) -> str | None:
    """Tell the agent the team knowledge exists and surface what it holds about this project."""
    project = project_name(str(payload.get("cwd", "")))
    found = await call_tool("kb_consult", {"query": f"{project} project", "limit": 5})
    results = found.get("results") or []
    intro = (
        "A shared team knowledge base is connected (tools kb_consult, kb_remember, "
        "kb_report_session). Consult it before starting a task and remember reusable findings."
    )
    body = format_results(results, f"{intro}\nWhat it holds about {project}:") if results else intro
    return _context("SessionStart", body)


async def prompt_submit(payload: dict[str, Any]) -> str | None:
    """Search with the request when it is substantial; stay silent when nothing relevant exists."""
    prompt = str(payload.get("prompt", "")).strip()
    if prompt.startswith("/") or len(prompt.split()) < MIN_PROMPT_WORDS:
        return None
    found = await call_tool("kb_consult", {"query": prompt[:MAX_PROMPT_CHARS], "limit": 3})
    results = found.get("results") or []
    if not results:
        return None
    return _context(
        "UserPromptSubmit", format_results(results, "Possibly relevant team knowledge:")
    )


async def _report(tool: str, payload: dict[str, Any], reason: str) -> None:
    path = str(payload.get("transcript_path", ""))
    turns = read_turns(path)
    if not turns:
        return
    project = project_name(str(payload.get("cwd", "")))
    session = str(payload.get("session_id", "")) or "unknown"
    activity = read_activity(path)
    await call_tool(
        tool,
        {
            "summary": await summarize(turns, activity, project, reason),
            "log": render_log(turns),
            "outcomes": outcomes(activity),
            "idempotency_key": f"hook-{tool}-{session}-{len(turns)}"[:120],
        },
        deadline=15.0,
    )


async def pre_compact(payload: dict[str, Any]) -> str | None:
    await _report("kb_report_compaction", payload, str(payload.get("trigger", "")))
    return None


async def session_end(payload: dict[str, Any]) -> str | None:
    await _report("kb_report_session", payload, str(payload.get("reason", "")))
    return None


HANDLERS = {
    "session-start": session_start,
    "prompt": prompt_submit,
    "pre-compact": pre_compact,
    "session-end": session_end,
}


async def run(event: str, stdin_text: str) -> str | None:
    """Output for the hook, or None. Failures are logged (with HIVE_DEBUG) and swallowed."""
    handler = HANDLERS.get(event)
    if handler is None:
        return None
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
        return await handler(payload if isinstance(payload, dict) else {})
    except Exception as exc:
        if os.environ.get("HIVE_DEBUG"):
            log.warning("hook %s failed: %r", event, exc)
        return None
