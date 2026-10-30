"""File writers for the export: JSONL (no dependencies) and Parquet (needs pyarrow).

Files are created with mode 0600 (they may hold private text) and are hashed after closing."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from typing import IO, TYPE_CHECKING, Any, Protocol

from hive_mcp.domain.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

CHUNK = 1 << 20


class RowWriter(Protocol):
    def write(self, rows: Sequence[dict[str, Any]]) -> None: ...

    def close(self) -> None: ...


def _open_private(path: Path) -> IO[bytes]:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "wb")


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)


class JsonlWriter:
    def __init__(self, path: Path, columns: Sequence[tuple[str, str]]) -> None:
        del columns  # JSON rows carry their own keys
        self._fh = _open_private(path)

    def write(self, rows: Sequence[dict[str, Any]]) -> None:
        for row in rows:
            line = json.dumps(row, ensure_ascii=False, default=_json_default)
            self._fh.write(line.encode() + b"\n")

    def close(self) -> None:
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()


class ParquetWriter:
    def __init__(self, path: Path, columns: Sequence[tuple[str, str]]) -> None:
        pa, pq = _pyarrow()
        kinds = {
            "string": pa.string(),
            "list": pa.list_(pa.string()),
            "timestamp": pa.timestamp("us", tz="UTC"),
            "int": pa.int64(),
        }
        self._pa = pa
        self._schema = pa.schema([(name, kinds[kind]) for name, kind in columns])
        self._fh = _open_private(path)
        self._writer = pq.ParquetWriter(self._fh, self._schema, compression="zstd")

    def write(self, rows: Sequence[dict[str, Any]]) -> None:
        if rows:  # one row group per page keeps memory bounded
            self._writer.write_table(self._pa.Table.from_pylist(list(rows), schema=self._schema))

    def close(self) -> None:
        self._writer.close()
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()


def _pyarrow() -> tuple[Any, Any]:
    try:
        import pyarrow as pa  # noqa: PLC0415 - optional dependency
        import pyarrow.parquet as pq  # noqa: PLC0415
    except ImportError as exc:
        raise ValidationError(
            "--format parquet needs pyarrow: install it with `pip install 'hive-mcp[parquet]'`"
            " (or use --format jsonl)"
        ) from exc
    return pa, pq


def check_format(fmt: str) -> None:
    if fmt == "parquet":
        _pyarrow()
    elif fmt != "jsonl":
        raise ValidationError("format must be jsonl or parquet")


def make_writer(fmt: str, path: Path, columns: Sequence[tuple[str, str]]) -> RowWriter:
    return ParquetWriter(path, columns) if fmt == "parquet" else JsonlWriter(path, columns)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
