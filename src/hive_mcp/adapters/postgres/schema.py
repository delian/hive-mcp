"""Forward-only migrations, serialised by an advisory lock so several app instances can start together."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hive_mcp.domain.errors import HiveError

if TYPE_CHECKING:
    from psycopg import AsyncConnection

MIGRATION_LOCK = 727_274

MIGRATIONS = [
    """
    CREATE TABLE items (
        id text PRIMARY KEY, org_id text NOT NULL, kind text NOT NULL, title text NOT NULL,
        body text NOT NULL, scope_level smallint NOT NULL, scope_owner text NOT NULL,
        visibility text NOT NULL, status text NOT NULL, author_id text NOT NULL,
        created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL, revision integer NOT NULL,
        supersedes text, source text, content_hash text NOT NULL,
        tags text[] NOT NULL DEFAULT '{}'
    );
    CREATE INDEX items_order ON items (org_id, created_at DESC, id DESC);
    CREATE INDEX items_scope ON items (org_id, scope_level, scope_owner);
    CREATE INDEX items_tags ON items USING gin (tags);
    CREATE INDEX items_hash ON items (org_id, content_hash);
    CREATE TABLE idempotency (
        org_id text NOT NULL, author_id text NOT NULL, key text NOT NULL,
        fingerprint text NOT NULL, item_id text NOT NULL,
        PRIMARY KEY (org_id, author_id, key)
    );
    CREATE TABLE text_docs (
        item_id text PRIMARY KEY, org_id text NOT NULL, scope_level smallint NOT NULL,
        scope_owner text NOT NULL, visibility text NOT NULL, tsv tsvector NOT NULL
    );
    CREATE INDEX text_docs_tsv ON text_docs USING gin (tsv);
    CREATE INDEX text_docs_scope ON text_docs (org_id, scope_level, scope_owner);
    CREATE TABLE vec_models (
        model_id text PRIMARY KEY, dim integer NOT NULL, table_name text NOT NULL
    );
    CREATE TABLE jobs (
        seq bigserial PRIMARY KEY, id text NOT NULL UNIQUE, org_id text NOT NULL, kind text NOT NULL,
        payload jsonb NOT NULL, state text NOT NULL, attempts integer NOT NULL,
        max_attempts integer NOT NULL, run_after timestamptz NOT NULL, dedupe_key text,
        lease_owner text, lease_expires timestamptz, last_error text
    );
    CREATE INDEX jobs_runnable ON jobs (state, run_after);
    CREATE TABLE audit (
        seq bigserial PRIMARY KEY, org_id text NOT NULL, actor_id text NOT NULL, action text NOT NULL,
        target_id text, at timestamptz NOT NULL, details jsonb NOT NULL
    );
    CREATE INDEX audit_org ON audit (org_id, seq);
    """,
    """
    CREATE TABLE kv (
        namespace text NOT NULL, key text NOT NULL, value jsonb NOT NULL, expires_at timestamptz,
        PRIMARY KEY (namespace, key)
    );
    CREATE INDEX kv_expiry ON kv (expires_at);
    """,
    """
    CREATE TABLE promotions (
        id text PRIMARY KEY, org_id text NOT NULL, item_id text NOT NULL, requester_id text NOT NULL,
        scope_level smallint NOT NULL, scope_owner text NOT NULL, visibility text NOT NULL,
        item_revision integer NOT NULL, reason text NOT NULL, status text NOT NULL,
        created_at timestamptz NOT NULL, decided_by text, decided_at timestamptz, note text NOT NULL
    );
    CREATE UNIQUE INDEX promotions_one_pending ON promotions (item_id) WHERE status = 'pending';
    CREATE INDEX promotions_queue ON promotions (org_id, status, created_at, id);
    """,
    """
    CREATE TABLE edges (
        org_id text NOT NULL, src text NOT NULL, dst text NOT NULL, relation text NOT NULL,
        author_id text NOT NULL, created_at timestamptz NOT NULL,
        PRIMARY KEY (org_id, src, dst, relation)
    );
    CREATE INDEX edges_dst ON edges (org_id, dst);
    """,
    """
    CREATE INDEX items_source ON items (org_id, source) WHERE source IS NOT NULL;
    CREATE INDEX items_author ON items (org_id, author_id);
    """,
    """
    CREATE TABLE item_revisions (
        item_id text NOT NULL REFERENCES items(id) ON DELETE CASCADE, org_id text NOT NULL,
        revision integer NOT NULL, title text NOT NULL, body text NOT NULL, kind text NOT NULL,
        status text NOT NULL, tags text[] NOT NULL, scope_level smallint NOT NULL,
        scope_owner text NOT NULL, visibility text NOT NULL, item_author text NOT NULL,
        written_at timestamptz NOT NULL, replaced_at timestamptz NOT NULL, replaced_by text NOT NULL,
        PRIMARY KEY (item_id, revision)
    );
    """,
    """
    CREATE TABLE blobs (
        org_id text NOT NULL, sha256 text NOT NULL, size bigint NOT NULL, data bytea NOT NULL,
        PRIMARY KEY (org_id, sha256)
    );
    CREATE TABLE rate_buckets (key text PRIMARY KEY, tokens double precision NOT NULL, seen timestamptz NOT NULL);
    """,
]


async def migrate(conn: AsyncConnection[Any]) -> int:
    """Run pending migrations inside one transaction (Postgres DDL is transactional)."""
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
    ext = await (
        await conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
    ).fetchone()
    if ext is None:
        await conn.execute(
            "CREATE EXTENSION IF NOT EXISTS vector"
        )  # needs privileges / allow-listing
    await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version integer PRIMARY KEY)")
    cur = await conn.execute("SELECT coalesce(max(version), 0) AS v FROM schema_migrations")
    row = await cur.fetchone()
    current = int(row["v"]) if row else 0
    if current > len(MIGRATIONS):
        raise HiveError(
            f"database schema v{current} is newer than this software (v{len(MIGRATIONS)})"
        )
    for version in range(current, len(MIGRATIONS)):
        await conn.execute(MIGRATIONS[version])  # no parameters: multi-statement script
        await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version + 1,))
    return len(MIGRATIONS)
