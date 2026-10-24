"""In-memory BlobStore (per-org namespaces)."""

from __future__ import annotations

from hive_mcp.domain.errors import HiveError, NotFound, ValidationError
from hive_mcp.ports.blobs import MAX_BLOB_BYTES, BlobRef, sha256_hex


class MemoryBlobs:
    def __init__(self) -> None:
        self._blobs: dict[tuple[str, str], bytes] = {}

    async def put(self, org_id: str, data: bytes) -> BlobRef:
        if len(data) > MAX_BLOB_BYTES:
            raise ValidationError("blob too large")
        digest = sha256_hex(data)
        self._blobs[(org_id, digest)] = data
        return BlobRef(org_id, digest, len(data))

    async def get(self, ref: BlobRef) -> bytes:
        data = self._blobs.get((ref.org_id, ref.sha256))
        if data is None:
            raise NotFound(ref.sha256)
        if sha256_hex(data) != ref.sha256:
            raise HiveError("blob corrupted")
        return data

    async def exists(self, ref: BlobRef) -> bool:
        return (ref.org_id, ref.sha256) in self._blobs

    async def delete(self, ref: BlobRef) -> None:
        self._blobs.pop((ref.org_id, ref.sha256), None)
