# Administration

**Who is an admin:** role `admin` (`hive user add ... --role admin`, or a token created with `--role admin`).

| Tool | What |
|---|---|
| `kb_admin_usage` | items and characters stored per person (archived excluded), biggest first |
| `kb_admin_audit(actor, action, target, after_seq, limit)` | the audit trail, oldest first, 500 per page; page with the last `seq` |

Audit actions include `item.create/update/relocate/purge`, `promotion.request/approved/rejected/withdrawn/stale`,
`edge.add/remove`, `user.forget`. Events hold ids and short facts (kind, scope), never content.

**Quotas** (off by default, admins exempt): `HIVE_QUOTA_ITEMS_PER_USER`, `HIVE_QUOTA_CHARS_PER_USER`. They apply to
new items (including reports, ingested parts and merges), not to edits. The usage query is cached for 30 seconds
per person per process, so with several replicas a person can overshoot slightly. A refused write says what to
do (archive or purge, or ask an admin). Archiving frees quota.
