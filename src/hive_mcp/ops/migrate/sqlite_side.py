"""The SQLite profile as a migration source (read-only) or target (empty, schema created)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import TYPE_CHECKING, Any

from hive_mcp.adapters.fs.blobs import FsBlobs
from hive_mcp.adapters.sqlite.codec import parse_ts, ts
from hive_mcp.adapters.sqlite.db import Database
from hive_mcp.domain.errors import ValidationError
from hive_mcp.ops.migrate.tables import BY_NAME, Row, Table

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

CHUNK = 500
DB = "hive.db"


class SqliteSide:
    kind = "sqlite"

    def __init__(self, root: Path, *, target: bool) -> None:
        self.root, self._target = root, target
        path = root / DB
        if target:
            if path.exists():
                raise ValidationError(f"{path} already exists: migrate into an empty directory")
            root.mkdir(parents=True, exist_ok=True)
            Database(path).close()  # creates the schema
            self._conn = sqlite3.connect(path, isolation_level=None)
        else:
            if not path.exists():
                raise ValidationError(f"no database at {path}")
            self._conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._blobs = FsBlobs(root / "blobs") if target else None

    # ------------------------------------------------------------------ reading
    def describe(self) -> str:
        return f"SQLite at {self.root / DB}"

    def is_empty(self) -> bool:
        return not any(self.count(t) for t in BY_NAME) and next(self.blobs(), None) is None

    def count(self, table: str) -> int:
        return int(self._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608

    def orgs(self) -> list[str]:
        return [r[0] for r in self._conn.execute("SELECT DISTINCT org_id FROM items ORDER BY 1")]

    def read(self, table: Table) -> Iterator[Row]:
        tags: dict[str, list[str]] = {}
        if table.name == "items":
            for r in self._conn.execute("SELECT item_id, tag FROM item_tags"):
                tags.setdefault(r["item_id"], []).append(r["tag"])
        columns = [c for c in table.columns if not (table.name == "items" and c == "tags")]
        cursor = self._conn.execute(f"SELECT {', '.join(columns)} FROM {table.name}")  # noqa: S608
        for r in cursor:
            row: Row = dict(r)
            for name in table.times:
                row[name] = parse_ts(row[name]) if row[name] else None
            for name in table.jsons:
                row[name] = json.loads(row[name])
            if table.name == "items":
                row["tags"] = sorted(tags.get(row["id"], []))
            elif table.lists:
                row["tags"] = sorted(json.loads(row["tags"]))
            yield row

    def blobs(self) -> Iterator[tuple[str, str, bytes]]:
        root = self.root / "blobs"
        for org_dir in sorted(root.glob("*")) if root.is_dir() else []:
            org = bytes.fromhex(org_dir.name).decode()
            for path in sorted(org_dir.glob("*/*")):
                if len(path.name) == 64 and path.is_file():  # noqa: PLR2004 - sha-256 in hex
                    yield org, path.name, path.read_bytes()

    # ------------------------------------------------------------------ writing
    def write(self, table: Table, rows: list[Row]) -> None:
        self._conn.execute("BEGIN")
        for row in rows:
            values: dict[str, Any] = dict(row)
            for name in table.times:
                values[name] = ts(values[name]) if values[name] else None
            for name in table.jsons:
                values[name] = json.dumps(values[name], sort_keys=True)
            item_tags = values.pop("tags") if table.name == "items" else None
            if table.name == "item_revisions":
                values["tags"] = json.dumps(sorted(values["tags"]))
            columns = [c for c in table.columns if c in values]
            marks = ", ".join("?" for _ in columns)
            self._conn.execute(
                f"INSERT INTO {table.name} ({', '.join(columns)}) VALUES ({marks})",  # noqa: S608
                [values[c] for c in columns],
            )
            for tag in item_tags or []:
                self._conn.execute(
                    "INSERT INTO item_tags (item_id, tag) VALUES (?, ?)", (values["id"], tag)
                )
        self._conn.execute("COMMIT")

    def write_blob(self, org: str, sha: str, data: bytes) -> None:
        assert self._blobs is not None  # noqa: S101
        ref = asyncio.run(self._blobs.put(org, data))
        if ref.sha256 != sha:
            raise ValidationError(f"stored file {sha} changed while copying")

    def close(self) -> None:
        self._conn.close()
