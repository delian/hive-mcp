"""Filesystem BlobStore: content-addressed, per-org directories, atomic writes, verified reads."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from hive_mcp.domain.errors import HiveError, NotFound, ValidationError
from hive_mcp.ports.blobs import MAX_BLOB_BYTES, BlobRef, sha256_hex


class FsBlobs:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, ref: BlobRef) -> Path:
        # The org id is arbitrary text: hex-encode it so it can never form a path (no "../" tricks).
        org_dir = ref.org_id.encode().hex()
        if len(ref.sha256) != len(sha256_hex(b"")) or not all(
            c in "0123456789abcdef" for c in ref.sha256
        ):
            raise ValidationError("invalid blob hash")
        path = (self._root / org_dir / ref.sha256[:2] / ref.sha256).resolve()
        if not path.is_relative_to(self._root):
            raise ValidationError("invalid blob reference")
        return path

    async def put(self, org_id: str, data: bytes) -> BlobRef:
        if len(data) > MAX_BLOB_BYTES:
            raise ValidationError("blob too large")
        ref = BlobRef(org_id, sha256_hex(data), len(data))
        await asyncio.to_thread(self._write, self._path(ref), data)
        return ref

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            Path(tmp).replace(path)  # atomic: readers never see a partial file
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    async def get(self, ref: BlobRef) -> bytes:
        path = self._path(ref)
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise NotFound(ref.sha256) from exc
        if sha256_hex(data) != ref.sha256:
            raise HiveError("blob corrupted")
        return data

    async def exists(self, ref: BlobRef) -> bool:
        return await asyncio.to_thread(self._path(ref).exists)

    async def delete(self, ref: BlobRef) -> None:
        await asyncio.to_thread(self._path(ref).unlink, missing_ok=True)
