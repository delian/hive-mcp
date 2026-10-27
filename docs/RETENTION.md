# Deleting data

| | Hides | Removes | Who |
|---|---|---|---|
| `kb_update status=archived` | from search and lists | nothing | anyone who can edit the item |
| `kb_purge id confirm` | everything | the item, tags, search entries, vectors, links, promotion requests, idempotency keys, and the stored log file when no other item uses it | the item's author, or an admin |
| `hive forget --org O --user U [--include-shared] --yes` | | a person's private items (and shared ones with `--include-shared`; otherwise they are listed and left) | operator |
| `HIVE_RETENTION_RULES='[{"scope":"private","days":730}]'` | | by rule (below) | automatic, every 10 minutes |
| `HIVE_RETENTION_SESSION_DAYS=N` | | session and compaction reports older than N days, checked every 10 minutes (0 = keep forever; default) | automatic |

Purges are crash-safe: a cleanup job is queued first, so an interrupted purge is finished later (the item row goes
first, the leftovers right after). Every purge is audited (`item.purge`, `user.forget`) with ids and kinds, never
content; the audit log, promotion records for other people's items, and `hive user`/token entries still mention a
user id, so remove the user (`hive user remove`, `hive token revoke`) as part of offboarding. Backups of the
database keep purged data until they expire; plan backup retention accordingly.
Not covered yet: purging an item's *older revisions* (there is no revision history, edits overwrite), and
deleting data from external embedding providers (nothing is stored there by this server).

## Retention rules

`HIVE_RETENTION_RULES` is a JSON list; each rule names a `kind` (`note`, `session`, ...) and/or a `scope`
(`private`, `project`, `team`, `org`) and `days`: `[{"kind": "session", "days": 90}, {"scope": "private", "days": 730}]`.
Items matching **any** rule and **not changed** for that many days are purged (item, search entries, vectors, links,
promotion requests, stored files) with the same crash-safe cleanup as `kb_purge`; an edit restarts the clock. A rule
without a kind or a scope is refused, as is any unknown key, and a bad rule stops the server at startup. A run purges
at most 500 items, so a first run on a large base is spread over several 10-minute sweeps.

Rules delete for good. The reversible path for stale knowledge is the owner's review (`kb_review_stale`, `kb_confirm`,
archive). Preview what the rules would remove with `hive retention`, apply them at once with `hive retention --yes`.

## Erasing a person

`hive forget --org O --user U [--include-shared] --erase-audit --yes` also replaces the person's id in the audit log
(as actor and as target) with one random alias, so the trail stays coherent but no longer names them (a hash would
be guessable from a list of emails; the alias is random and stored nowhere). Still naming the person afterwards:
promotion records and item history of *other people's* items they touched, and the `details` text of audit events.
