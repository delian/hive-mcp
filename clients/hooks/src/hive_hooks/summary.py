"""Summaries of a session for the knowledge base, without a language model in the loop.

The default picks the most informative sentences the agent wrote (changes, decisions, causes,
numbers) and adds what it did to the world (files edited, commits made). A user who has a model
at hand can plug it in: HIVE_HOOK_SUMMARIZER names a command that reads the conversation on
stdin and prints a summary; if it fails or is slow, the extractive summary is used.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
from typing import TYPE_CHECKING

from hive_hooks.transcript import render_log

if TYPE_CHECKING:
    from hive_hooks.transcript import Activity, Turn

MAX_SUMMARY = 1_500
MAX_EXTERNAL = 2_000
MAX_INPUT = 30_000
DEFAULT_TIMEOUT = 20.0
MAX_OUTCOMES = 8
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
_SIGNALS = re.compile(
    r"\b(fixed|added|implemented|created|removed|replaced|changed|renamed|decided|chose|because|"
    r"root cause|found|result|measured|works|passes|passed|failed|error|bug|should|must|need|"
    r"instead|however|turns out|confirmed|verified|caused|resolved|deployed|migrated)\b",
    re.I,
)
_FILLER = re.compile(
    r"^(sure|okay|ok|great|let me|let's|i'll|i will|now (i|let)|looking at|first,? |next,? |"
    r"here'?s|here is|i('m| am) (going|now)|alright|perfect|done[.!])",
    re.I,
)
_CODEY = re.compile(r"[{}();=<>]{3,}|^\s*(def|class|import|from|return|if|for)\b|```")


def _sentences(turns: list[Turn]) -> list[tuple[int, int, str]]:
    """(turn index, position in turn, sentence) for the agent's prose."""
    found = []
    for t, turn in enumerate(turns):
        if turn.role != "assistant":
            continue
        for p, raw in enumerate(_SENTENCES.split(turn.text)):
            text = " ".join(raw.split()).lstrip("-*#> ").strip()
            if 25 <= len(text) <= 300 and not _CODEY.search(text) and not _FILLER.match(text):  # noqa: PLR2004
                found.append((t, p, text))
    return found


def _score(sentence: str, turn: int, turns: int, last_turn: bool) -> float:
    score = 2.0 * len(_SIGNALS.findall(sentence))
    score += 1.0 if re.search(r"\d", sentence) else 0.0
    score += 0.5 * min(len(re.findall(r"`[^`]+`", sentence)), 2)
    score += 1.5 * (turn + 1) / turns  # later work is the outcome, earlier work the exploration
    return score * (1.5 if last_turn else 1.0)


def key_points(turns: list[Turn], budget: int) -> list[str]:
    sentences = _sentences(turns)
    if not sentences:
        return []
    last = max(t for t, _, _ in sentences)
    ranked = sorted(sentences, key=lambda s: -_score(s[2], s[0], len(turns), s[0] == last))
    chosen: list[tuple[int, int, str]] = []
    used = 0
    for item in ranked:
        if item[2] in {c[2] for c in chosen} or used + len(item[2]) + 4 > budget:
            continue
        chosen.append(item)
        used += len(item[2]) + 4
        if len(chosen) == 5:  # noqa: PLR2004
            break
    return [text for _, _, text in sorted(chosen)]  # in the order they happened


def outcomes(activity: Activity) -> list[str]:
    items = [f"commit: {message}" for message in activity.commits[:5]]
    if activity.files:
        shown = ", ".join(activity.files[:4])
        more = f" and {len(activity.files) - 4} more" if len(activity.files) > 4 else ""  # noqa: PLR2004
        items.append(f"edited {len(activity.files)} file(s): {shown}{more}")
    return items[:MAX_OUTCOMES]


def extractive_summary(
    turns: list[Turn], activity: Activity, project: str, reason: str = ""
) -> str:
    users = [t for t in turns if t.role == "user"]
    head = f"Session in {project}: {len(users)} request(s)"
    head += f" (ended: {reason})." if reason else "."
    lines = [head]
    if users:
        lines.append(f"Asked: {' '.join(users[0].text.split())[:300]}")
    points = key_points(turns, budget=700)
    if points:
        lines.append("Key points:")
        lines += [f"- {p}" for p in points]
    elif turns and turns[-1].role == "assistant":
        lines.append(f"Last reply: {' '.join(turns[-1].text.split())[:400]}")
    lines += [f"{o[0].upper()}{o[1:]}" for o in outcomes(activity)]
    return "\n".join(lines)[:MAX_SUMMARY]


async def external_summary(turns: list[Turn]) -> str | None:
    """The user's own summarizer, if configured and it answers in time; None otherwise."""
    command = os.environ.get("HIVE_HOOK_SUMMARIZER", "").strip()
    if not command:
        return None
    process = None
    try:
        timeout = min(float(os.environ.get("HIVE_HOOK_SUMMARIZER_TIMEOUT", DEFAULT_TIMEOUT)), 25.0)
        process = await asyncio.create_subprocess_exec(
            *shlex.split(command),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(
            process.communicate(render_log(turns, MAX_INPUT).encode()), timeout
        )
    except (OSError, ValueError, TimeoutError):
        if process and process.returncode is None:
            process.kill()  # a summarizer that is too slow must not outlive the hook
            await process.wait()
        return None
    if process.returncode != 0:
        return None
    text = out.decode("utf-8", errors="replace").strip()
    return text[:MAX_EXTERNAL] or None


async def summarize(turns: list[Turn], activity: Activity, project: str, reason: str = "") -> str:
    fallback = extractive_summary(turns, activity, project, reason)
    external = await external_summary(turns)
    if external is None:
        return fallback
    return f"Session in {project}{f' (ended: {reason})' if reason else ''}.\n{external}"[
        :MAX_EXTERNAL
    ]
