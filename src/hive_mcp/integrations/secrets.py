"""Encrypting integration secrets (signing secrets, bot tokens) before they are stored.

AES-256-GCM with a key derived from the server's secret key (HKDF), so the database alone does
not reveal them; the key lives in HIVE_SECRET_KEY or the owner-only secret.key file. The secret's
field name is authenticated with it, so a value cannot be moved to another field.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from hive_mcp.domain.errors import ValidationError

NONCE_BYTES = 12


class SecretBox:
    def __init__(self, master_key: bytes) -> None:
        key = HKDF(hashes.SHA256(), 32, salt=None, info=b"hive-integration-secrets").derive(
            master_key
        )
        self._aead = AESGCM(key)

    def seal(self, field: str, value: str) -> str:
        nonce = os.urandom(NONCE_BYTES)
        blob = nonce + self._aead.encrypt(nonce, value.encode(), field.encode())
        return base64.urlsafe_b64encode(blob).decode()

    def open(self, field: str, sealed: str) -> str:
        try:
            blob = base64.urlsafe_b64decode(sealed.encode())
            return self._aead.decrypt(
                blob[:NONCE_BYTES], blob[NONCE_BYTES:], field.encode()
            ).decode()
        except (InvalidTag, ValueError) as exc:
            raise ValidationError(
                f"the stored {field} cannot be read (was HIVE_SECRET_KEY changed?); set it again"
            ) from exc
