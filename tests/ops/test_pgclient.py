"""pg_dump / pg_restore older than the server are refused up front with an actionable message."""

from __future__ import annotations

import pytest

from hive_mcp.domain.errors import ValidationError
from hive_mcp.ops import pgclient


def patch(monkeypatch, client, server):
    monkeypatch.setattr(pgclient, "client_major", lambda tool: client)
    monkeypatch.setattr(pgclient, "server_major", lambda dsn: server)


def test_an_older_client_is_refused_and_the_message_says_what_to_install(monkeypatch):
    patch(monkeypatch, 15, 17)
    with pytest.raises(ValidationError) as caught:
        pgclient.check_client("/usr/bin/pg_dump", "host=x")
    assert str(caught.value) == (
        "pg_dump is version 15 but the database server is version 17: install the PostgreSQL 17 "
        "client tools (for example `apt install postgresql-client-17`) and put them first on PATH"
    )


@pytest.mark.parametrize(("client", "server"), [(16, 16), (17, 16), (16, None), (None, 16)])
def test_equal_newer_or_unknown_versions_pass(monkeypatch, client, server):
    patch(monkeypatch, client, server)
    pgclient.check_client("pg_dump", "host=x")


def test_the_version_is_read_from_the_tools_own_output(tmp_path):
    tool = tmp_path / "pg_dump"
    tool.write_text("#!/bin/sh\necho 'pg_dump (PostgreSQL) 16.4 (Debian 16.4-1.pgdg120+1)'\n")
    tool.chmod(0o755)
    assert pgclient.client_major(str(tool)) == 16
    assert pgclient.client_major(str(tmp_path / "missing")) is None


def test_an_unreachable_server_is_not_a_version_problem():
    assert pgclient.server_major("host=/nonexistent port=1 connect_timeout=1") is None
