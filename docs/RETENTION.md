# Deleting data

| | Hides | Removes | Who |
|---|---|---|---|
| `kb_update status=archived` | from search and lists | nothing | anyone who can edit the item |
| `kb_purge id confirm` | everything | the item, tags, search entries, vectors, links, promotion requests, idempotency keys, and the stored log file when no other item uses it | the item's author, or an admin |
| `hive forget --org O --user U [--include-shared] --yes` | | a person's private items (and shared ones with `--include-shared`; otherwise they are listed and left) | operator |
| `HIVE_RETENTION_SESSION_DAYS=N` | | session and compaction reports older than N days, checked every 10 minutes (0 = keep forever; default) | automatic |

Purges are crash-safe: a cleanup job is queued first, so an interrupted purge is finished later (the item row goes
first, the leftovers right after). Every purge is audited (`item.purge`, `user.forget`) with ids and kinds, never
content; the audit log, promotion records for other people's items, and `hive user`/token entries still mention a
user id, so remove the user (`hive user remove`, `hive token revoke`) as part of offboarding. Backups of the
database keep purged data until they expire; plan backup retention accordingly.
Not covered yet: purging an item's *older revisions* (there is no revision history, edits overwrite), and
deleting data from external embedding providers (nothing is stored there by this server).
