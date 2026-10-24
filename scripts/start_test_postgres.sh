#!/bin/bash
# Start a throwaway local Postgres (needs postgresql-16 + pgvector installed) for HIVE_TEST_PG_DSN tests.
# Usage: scripts/start_test_postgres.sh && export HIVE_TEST_PG_DSN="host=/tmp port=55432 dbname=hive_test user=postgres"
set -e
BIN=/usr/lib/postgresql/16/bin
DATA=${PGDATA_DIR:-/tmp/pgdata}
if ! su postgres -c "$BIN/pg_ctl -D $DATA status" >/dev/null 2>&1; then
  [ -d "$DATA" ] || { mkdir -p "$DATA" && chown postgres "$DATA" && su postgres -c "$BIN/initdb -D $DATA -E UTF8 --auth=trust" >/dev/null; }
  su postgres -c "$BIN/pg_ctl -D $DATA -o \"-c port=55432 -c unix_socket_directories=/tmp -c listen_addresses='' -c fsync=off -c synchronous_commit=off -c shared_buffers=512MB\" -w -l /tmp/pg.log start" >/dev/null
fi
su postgres -c "$BIN/createdb -h /tmp -p 55432 hive_test" 2>/dev/null || true
su postgres -c "psql -h /tmp -p 55432 -d hive_test -qc 'CREATE EXTENSION IF NOT EXISTS vector'"
echo "ready: export HIVE_TEST_PG_DSN=\"host=/tmp port=55432 dbname=hive_test user=postgres\""
