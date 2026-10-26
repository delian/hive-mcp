# Digest: what is new

**Tool `kb_digest(days=7, limit=30, include_external=false)`** answers "what did the others add while I was away?":
items created in the last `days` days in places the caller can read, grouped by where they live (`project p1`, `team t1`,
`organisation-wide knowledge`), with author, kind, tags and id, the busiest tags, and (for approvers) how many promotion
requests wait. It leaves out the caller's own items and personal session/compaction logs. `include_external` adds knowledge
other groups shared organisation-wide, labelled `EXTERNAL`. The result is built with the caller's own read filter, so a
digest never shows more than a search would. `hive digest --user ann@acme.com` prints the same for a person.

Items *edited* in the window are not listed (the newest-first order is by creation). Very busy knowledge bases: at most 2,000
items are examined per digest (the text says so when that limit was hit).

## Weekly email
Needs SMTP (`HIVE_SMTP_HOST`, `HIVE_SMTP_FROM`, see PROMOTION.md) and:

| Setting | Meaning |
|---|---|
| `HIVE_DIGEST_WEEKDAY` | 0 = Monday ... 6 = Sunday; unset = no emails |
| `HIVE_DIGEST_HOUR_UTC` | send from this hour (UTC), default 8 |

Every directory user (`hive user list`, not disabled) gets **their own** digest by email: titles, origins and ids, never item
text, and only items they may read. People with nothing new (and no pending approvals) get nothing. A claim per person and ISO
week in the key-value store keeps it to one email per person per week even with several replicas or after a restart; a failed
send releases its claim and is retried at the next check (every 10 minutes). `hive digest --send` sends this week's emails now
(people already sent are skipped).

The digest is **not** posted to the chat webhook: a shared channel would show every member the titles of things only some of
them may read.
