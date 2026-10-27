"""Encrypting integration secrets (signing secrets, bot tokens) before they are stored.

AES-256-GCM with a key derived from the server's secret key (HKDF), so the database alone does
not reveal them; the key lives in HIVE_SECRET_KEY or the owner-only secret.key file. The secret's
field name is authenticated with it, so a value cannot be moved to another field.
"""

from __future__ import annotations

import base64
import os
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from hive_mcp.domain.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable

NONCE_BYTES = 12


class SecretBox:
    """`master_key` may be a function: the key file is then read on first use, so a command that
    never touches integration secrets does not need (or trip over) it."""

    def __init__(self, master_key: bytes | Callable[[], bytes]) -> None:
        self._master = master_key
        self._cached: AESGCM | None = None

    @property
    def _aead(self) -> AESGCM:
        if self._cached is None:
            master = self._master() if callable(self._master) else self._master
            info = b"hive-integration-secrets"
            self._cached = AESGCM(HKDF(hashes.SHA256(), 32, salt=None, info=info).derive(master))
        return self._cached

    def seal(self, field: str, value: str) -> str:
        nonce = os.urandom(NONCE_BYTES)
        blob = nonce + self._aead.encrypt(nonce, value.encode(), field.encode())
        return base64.urlsafe_b64encode(blob).decode()

    def open(self, field: str, sealed: str) -> str:
        try:
            blob = base64.urlsafe_b64decode(sealed.encode())
            plain = self._aead.decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], field.encode())
            return plain.decode()
        except (InvalidTag, ValueError) as exc:
            raise ValidationError(
                f"the stored {field} cannot be read (was HIVE_SECRET_KEY changed?); set it again"
            ) from exc
