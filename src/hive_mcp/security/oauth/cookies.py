"""Binds a browser to its login transaction with a signed cookie (defence against login CSRF)."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

COOKIE_NAME = "hive_oauth"


def load_secret_key(configured: str | None, path: Path) -> bytes:
    """The configured key, else one generated once and kept in an owner-only file next to the data."""
    if configured:
        return configured.encode()
    if path.exists():
        return bytes.fromhex(path.read_text().strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
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
