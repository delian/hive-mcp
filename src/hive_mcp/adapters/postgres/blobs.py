"""PostgreSQL BlobStore: content-addressed rows, so every replica sees the same files and one
pg_dump covers them. Per-org keys keep dedupe from leaking across tenants."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.domain.errors import HiveError, NotFound, ValidationError
from hive_mcp.ports.blobs import MAX_BLOB_BYTES, BlobRef, sha256_hex

if TYPE_CHECKING:
    from hive_mcp.adapters.postgres.db import PgDatabase


class PgBlobs:
    def __init__(self, db: PgDatabase) -> None:
        self._db = db

    async def put(self, org_id: str, data: bytes) -> BlobRef:
        if len(data) > MAX_BLOB_BYTES:
            raise ValidationError("blob too large")
        ref = BlobRef(org_id, sha256_hex(data), len(data))
        async with self._db.tx() as conn:
            await conn.execute(
                "INSERT INTO blobs (org_id, sha256, size, data) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (org_id, ref.sha256, ref.size, data),
            )
        return ref

    async def get(self, ref: BlobRef) -> bytes:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "SELECT data FROM blobs WHERE org_id=%s AND sha256=%s", (ref.org_id, ref.sha256)
            )
            row = await cur.fetchone()
        if row is None:
            raise NotFound(ref.sha256)
        data = bytes(row["data"])
        if sha256_hex(data) != ref.sha256:
            raise HiveError("blob corrupted")
        return data

    async def exists(self, ref: BlobRef) -> bool:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "SELECT 1 FROM blobs WHERE org_id=%s AND sha256=%s", (ref.org_id, ref.sha256)
            )
            return await cur.fetchone() is not None

    async def delete(self, ref: BlobRef) -> None:
        async with self._db.tx() as conn:
            await conn.execute(
                "DELETE FROM blobs WHERE org_id=%s AND sha256=%s", (ref.org_id, ref.sha256)
            )
