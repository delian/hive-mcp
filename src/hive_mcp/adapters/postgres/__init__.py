"""PostgreSQL adapters (external profile): pgvector, full-text search, SKIP LOCKED job queue."""

from hive_mcp.adapters.postgres.backend import PostgresBackend

__all__ = ["PostgresBackend"]
