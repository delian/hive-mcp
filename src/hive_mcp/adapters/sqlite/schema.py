"""Forward-only migrations. The app refuses to open a database newer than it understands."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.domain.errors import HiveError

if TYPE_CHECKING:
    import sqlite3

MIGRATIONS = [
    """
    CREATE TABLE items (
        id TEXT PRIMARY KEY, org_id TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
        body TEXT NOT NULL, scope_level INTEGER NOT NULL, scope_owner TEXT NOT NULL,
        visibility TEXT NOT NULL, status TEXT NOT NULL, author_id TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL,
        supersedes TEXT, source TEXT, content_hash TEXT NOT NULL
    );
    CREATE INDEX items_order ON items (org_id, created_at, id);
    CREATE INDEX items_scope ON items (org_id, scope_level, scope_owner);
    CREATE INDEX items_hash ON items (org_id, content_hash);
    CREATE TABLE item_tags (
        item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE, tag TEXT NOT NULL,
        PRIMARY KEY (item_id, tag)
    );
    CREATE INDEX item_tags_tag ON item_tags (tag);
    CREATE TABLE idempotency (
        org_id TEXT NOT NULL, author_id TEXT NOT NULL, key TEXT NOT NULL,
        fingerprint TEXT NOT NULL, item_id TEXT NOT NULL,
        PRIMARY KEY (org_id, author_id, key)
    );
    CREATE TABLE text_meta (
        rowid INTEGER PRIMARY KEY, item_id TEXT NOT NULL UNIQUE, org_id TEXT NOT NULL,
        scope_level INTEGER NOT NULL, scope_owner TEXT NOT NULL, visibility TEXT NOT NULL
    );
    CREATE VIRTUAL TABLE text_fts USING fts5(tokens, tokenize = 'unicode61 remove_diacritics 0');
    CREATE TABLE vec_models (model_id TEXT PRIMARY KEY, dim INTEGER NOT NULL);
    CREATE TABLE vec_chunks (
        item_id TEXT NOT NULL, model_id TEXT NOT NULL, ord INTEGER NOT NULL, org_id TEXT NOT NULL,
        scope_level INTEGER NOT NULL, scope_owner TEXT NOT NULL, visibility TEXT NOT NULL,
        vec BLOB NOT NULL, PRIMARY KEY (item_id, model_id, ord)
    );
    CREATE INDEX vec_chunks_model ON vec_chunks (model_id, org_id);
    CREATE TABLE jobs (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, org_id TEXT NOT NULL,
        kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL,
        max_attempts INTEGER NOT NULL, run_after TEXT NOT NULL, dedupe_key TEXT, lease_owner TEXT,
        lease_expires TEXT, last_error TEXT
    );
    CREATE INDEX jobs_runnable ON jobs (state, run_after);
    CREATE TABLE audit (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, actor_id TEXT NOT NULL,
        action TEXT NOT NULL, target_id TEXT, at TEXT NOT NULL, details TEXT NOT NULL
    );
    CREATE INDEX audit_org ON audit (org_id, seq);
    """,
    """
    CREATE TABLE kv (
        namespace TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, expires_at TEXT,
        PRIMARY KEY (namespace, key)
    );
    CREATE INDEX kv_expiry ON kv (expires_at);
    """,
]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations (each in its own transaction); returns the schema version."""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > len(MIGRATIONS):
        raise HiveError(
            f"database schema v{current} is newer than this software (v{len(MIGRATIONS)})"
        )
    for version in range(current, len(MIGRATIONS)):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in MIGRATIONS[version].split(";\n"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version + 1}")  # int formatted, not user input
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return len(MIGRATIONS)
