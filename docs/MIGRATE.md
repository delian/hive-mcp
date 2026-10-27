# Moving between SQLite and PostgreSQL

`hive migrate` copies a whole knowledge base from one profile to the other and **proves the copy** before it says so. The
source is only read (SQLite opened read-only, Postgres in a read-only transaction); it is never changed, so the old
deployment is your fallback until you delete it.

```bash
# SQLite (HIVE_DATA_DIR) -> a PostgreSQL you run (empty database, pgvector available)
hive migrate to-postgres --database-url "postgresql://hive:...@db.example.com/hive"

# SQLite -> the PostgreSQL inside the single-container image (built with WITH_POSTGRES=true)
hive migrate to-postgres --embedded

# PostgreSQL (HIVE_DATABASE_URL) -> an empty SQLite directory
hive migrate to-sqlite --data-dir /data-new
```
Add `--dry-run` first: it counts what would be copied and writes nothing (it does not look at the target).

## Procedure
1. **Stop the server** (or any writer). A write during the copy makes the proof fail, which is its job.
2. Take a backup anyway (`hive backup`, docs/BACKUP.md).
3. Run the command. It refuses a target that already holds data, and the source and target being the same database.
4. It copies, **reads both stores back through the same code and compares them** (row counts and an order-independent
   checksum per table, plus the stored files), and exits non-zero naming the table that differs. Only then does it build
   the search indexes on the target (it embeds every note with `HIVE_EMBEDDER`; `--no-reindex` leaves that to
   `hive reindex --org <id>` later).
5. Start the server on the new profile: `HIVE_PROFILE=external HIVE_DATABASE_URL=...`, or `HIVE_EMBEDDED_POSTGRES=true`, or
   `HIVE_DATA_DIR=/data-new` for SQLite. Keep `HIVE_SECRET_KEY` (or the `secret.key` file) the same: integration secrets
   are sealed with it. `to-sqlite` copies `tokens.json`, `users.json` and `secret.key` into the new directory; the other
   direction uses the same data directory, so they stay where they are.

## What is copied
Items with their tags, every earlier version, idempotency keys, the audit trail (with its numbering), key-value records
(admin-managed integration settings, connector state, confirmations, ...), promotions (including decided ones), graph edges
and stored files. **Not copied**: the search and vector indexes (rebuilt in step 4), queued background jobs (they are
derived from the data: indexing, and notifications that had not been sent yet), rate-limit buckets and records that had
already expired.

## Limits and honesty
- Archives made by `hive backup` cannot be restored into the other profile; use this command instead.
- Tested here SQLite -> PostgreSQL 16 -> SQLite with a varied dataset (scopes, tags, edited notes, a link, a pending
  promotion, a stored file, settings records), including the embedded PostgreSQL path, with identical checksums and working
  search on both ends. Not tested on a large knowledge base: the copy streams rows but holds the tag index of the items
  table in memory, and indexing embeds every note once, which takes as long as `hive reindex`.
- `HIVE_VECTOR_STORE=lancedb` (embedded profile only) is not populated by this command; run `hive reindex` after switching
  to it.
