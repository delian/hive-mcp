"""Binds a browser to its login transaction with a signed cookie (defence against login CSRF)."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import ValidationError

if TYPE_CHECKING:
    from pathlib import Path

COOKIE_NAME = "hive_oauth"
KEY_BYTES = 32  # pragma: no mutate - size of the generated key


def load_secret_key(configured: str | None, path: Path) -> bytes:
    """The configured key, else one generated once and kept in an owner-only file next to the data."""
    if configured:
        return configured.encode()
    if path.exists():
        try:
            key = bytes.fromhex(path.read_text().strip())
        except ValueError:
            key = b""
        if len(key) < KEY_BYTES:  # a crash while the file was being written: never sign with it
            raise ValidationError(f"{path} is empty or damaged; delete it to generate a new key")
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(KEY_BYTES)
    fd = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )  # never readable by others, even briefly
    with os.fdopen(fd, "w") as handle:
        handle.write(key.hex())
    return key


class CookieSigner:
    def __init__(self, key: bytes) -> None:
        self._key = key

    def _mac(self, txn_id: str) -> str:
        return hmac.new(self._key, f"hive-oauth-txn:{txn_id}".encode(), hashlib.sha256).hexdigest()

    def sign(self, txn_id: str) -> str:
        return f"{txn_id}.{self._mac(txn_id)}"

    def verify(self, cookie: str | None, txn_id: str) -> bool:
        """True only if the cookie was issued by this server *for this exact transaction*."""
        if not cookie or not txn_id:
            return False
        return hmac.compare_digest(
            cookie.encode(), self.sign(txn_id).encode()
        )  # bytes: str with non-ASCII raises
