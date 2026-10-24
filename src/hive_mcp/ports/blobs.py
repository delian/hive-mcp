"""Content-addressed file storage, namespaced per org so dedupe cannot leak across tenants."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

MAX_BLOB_BYTES = 512 * 1024 * 1024


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class BlobRef:
    org_id: str
    sha256: str
    size: int


class BlobStore(Protocol):
    async def put(self, org_id: str, data: bytes) -> BlobRef:
        """Idempotent. Raises ValidationError above MAX_BLOB_BYTES."""

    async def get(self, ref: BlobRef) -> bytes:
        """Verifies the hash on read; raises NotFound if missing, HiveError on corruption."""

    async def exists(self, ref: BlobRef) -> bool: ...

    async def delete(self, ref: BlobRef) -> None: ...
