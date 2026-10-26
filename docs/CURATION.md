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
