# Curation tools

The server has no language model, so it never writes summaries itself: the calling agent reads, decides and writes;
the server makes the change safely (access checks, revisions, links, audit).

| Tool | Does |
|---|---|
| `kb_retag(ids, add_tags, remove_tags)` | same tag change on up to 100 items; per-item result (changed / unchanged / failed) |
| `kb_rename_tag(old, new, dry_run=true)` | rename a tag on every item you can edit (up to 1000, archived included); dry run first |
| `kb_similar(id)` | items closest in meaning (spot duplicates, find related notes); `likely_duplicate` at score >= 0.9 |
| `kb_merge(ids, title, body, ...)` | 2-20 items -> one new item the agent wrote; links `supersedes`; sources become superseded/archived |

Rules: you need edit rights on every item you change or merge (nothing is stored if any source is refused). A merge
stays in the sources' shared place only if they all share it and you may publish there directly; otherwise the result
is private (use `kb_request_promotion`). Retrying an identical merge returns the same item.
Suggested agent routine: `kb_similar` -> `kb_get` each -> write the merged text -> `kb_merge`.

## Browsing
`kb_list(kinds, tags, status, mine_only, limit, cursor)` pages through items newest first (stable even when items
share a timestamp); `kb_tags(prefix, limit)` lists tags in use with counts among items you can see. Look at
`kb_tags` before tagging so agents reuse existing tags. MCP clients can also use the prompts (`consult_before_task`,
`end_of_session_report`, `before_compaction`, `tidy_tag`, `review_promotions`) and resources
(`kb://guide`, `kb://recent`, `kb://item/{id}`).

## Keeping knowledge trustworthy

**Duplicates.** `kb_duplicates` sweeps the notes you can edit (50 per call; `next_cursor` continues) and
returns groups that say nearly the same thing (`threshold` 0.5-1.0, default 0.9; `exact` marks identical
text). It changes nothing: read a group, write one consolidated note, and `kb_merge` it (the sources are
marked superseded and linked, so history stays).

**Stale notes.** A note is stale when neither an edit nor a confirmation is newer than the window
(default 180 days; session and compaction logs are exempt). `kb_review_stale` lists yours
(`mine_only=false`: anything you can edit); for each, `kb_confirm` (vouch it is still true: recorded in the
key-value store and the audit log, no new revision), `kb_update` (fix) or `kb_update status=archived`
(retire). With `HIVE_STALE_REVIEW_DAYS=N` the weekly digest email gets a "Still true?" section listing each
author's own stale notes, so nobody has to remember to look.
