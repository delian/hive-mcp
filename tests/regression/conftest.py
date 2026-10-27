"""Regression tests reuse the service stack (memory, SQLite and, with HIVE_TEST_PG_DSN, Postgres)."""

from tests.services.conftest import stack  # noqa: F401 - the same service stack as the service tests
