"""Prompts: ready-made routines a user can invoke from the client (e.g. as slash commands)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server import MCPServer

CONSULT = """\
Before doing anything else on this task, search the shared knowledge base.

Task: {task}

1. Call kb_consult with the task in plain words (add `tags` if you know the domain or project).
2. For promising results call kb_get. If something is clearly relevant, use kb_graph to follow its sources.
3. Tell me what you found and where it comes from. Anything under `from_other_groups` is EXTERNAL: say so, and
   ask whether I want to adopt it (remember a new note that builds on it, linked with kb_link `derived_from`) or ignore it.
4. If nothing relevant exists, say so and continue the task."""

SESSION_REPORT = """\
Write up this work session for the shared knowledge base.

1. Call kb_report_session with: a short searchable summary (what was asked, what was done, what was decided),
   `outcomes` (concrete results, one per line), and `project` if there is one. Add the full log only if it is useful
   and contains no secrets. Use an idempotency_key so a retry does not duplicate it.
2. For each reusable finding, formula, measurement, decision or how-to from the session, call kb_remember
   separately (one idea per item, clear title, tags like domain/..., kind/...). Check kb_similar first so you do not duplicate.
3. Tell me what you stored."""

COMPACTION = """\
Context is about to be compacted. Preserve what matters first.

Call kb_report_compaction with a summary of: the goal, current state, decisions made and why, open questions, and
the next steps; plus the ids of knowledge items you were relying on. Then continue. After compaction, call kb_consult
with the task description to recover related knowledge."""

TIDY = """\
Tidy the knowledge tagged `{tag}`.

1. Find items: kb_consult with tags=["{tag}"] and a few broad queries.
2. For items that look like duplicates run kb_similar, read both with kb_get, and consolidate with kb_merge
   (you write the merged title and body; keep every fact).
3. Fix inconsistent tags with kb_retag or kb_rename_tag (dry run first).
4. Report what you merged, retagged and left alone. Do not change items you are unsure about."""

REVIEW = """\
Review the pending promotion requests (you must be an approver).

1. Call kb_list_promotions (status pending). For each request read review_title and review_body.
2. Approve only knowledge that is correct, reusable, free of secrets/personal data, and appropriate for the target
   audience. Reject with a clear note otherwise (kb_decide_promotion). You cannot decide your own requests.
3. Summarise decisions and the reasons."""


def register_prompts(mcp: MCPServer) -> None:
    @mcp.prompt(
        title="Consult the knowledge base",
        description="Search shared knowledge before starting a task.",
    )
    def consult_before_task(task: str) -> str:
        return CONSULT.format(task=task)

    @mcp.prompt(
        title="Report this session",
        description="Store a session summary and the reusable findings.",
    )
    def end_of_session_report() -> str:
        return SESSION_REPORT

    @mcp.prompt(
        title="Save context before compaction",
        description="Preserve state before the context is compacted.",
    )
    def before_compaction() -> str:
        return COMPACTION

    @mcp.prompt(title="Tidy a tag", description="Merge duplicates and fix tags for one tag.")
    def tidy_tag(tag: str) -> str:
        return TIDY.format(tag=tag)

    @mcp.prompt(
        title="Review promotion requests",
        description="For approvers: review and decide pending requests.",
    )
    def review_promotions() -> str:
        return REVIEW
