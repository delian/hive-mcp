# Backup and restore

```
hive backup /backups/                 # or /backups/hive-2026-10-26.tar.gz; verified by reading it back
hive backup-verify /backups/x.tar.gz  # checksums + database integrity, restores nothing
hive restore /backups/x.tar.gz [--force]
```
`hive backup` is safe while the server runs: SQLite is copied with its online-backup API, Postgres with
`pg_dump` (the Docker image includes the client; the password is passed through the environment, not the
command line). The archive contains the database, the stored log files, `tokens.json`, `users.json` and the
cookie signing key (`secret.key`), each with a SHA-256 in a manifest. **It holds secrets (token hashes, the cookie
key, all knowledge in clear text): store and transfer it like the database itself.**

Restore: stop the server, set the same `HIVE_*` settings as the target deployment, run `hive restore`. It refuses
archives that fail verification, contain unexpected or unsafe entries, or come from the other profile
(embedded vs external). It never overwrites an existing database without `--force`; for Postgres, `--force` is
always required because the dump replaces the database's tables. The embedding model files are not part of the
backup (`hive models fetch`); search indexes live in the database and come back with it. The exception is the optional LanceDB vector store (`HIVE_VECTOR_STORE=lance`): its `lance/` directory is derived data and is not archived, so run `hive reindex` after a restore.

Routine: run `hive backup` from a scheduled job (Azure Container Apps job, cron, or Kubernetes CronJob) writing to
durable storage, keep several generations, and **practise a restore** into a scratch environment now and then.
For point-in-time recovery of the Postgres profile use the platform's own PITR (Azure Flexible Server keeps 14
days in the template) in addition. Purged data stays in backups until the backup expires
(see [RETENTION.md](RETENTION.md)).
