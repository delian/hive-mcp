"""Static bearer tokens for pipelines, the CLI and tests (OAuth serves interactive assistants).

Only SHA-256 hashes are stored; the plaintext token is shown once. Tokens can expire and be
revoked by deleting their record; the file is re-read when it changes, so no restart is needed.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from mcp.server.auth.provider import AccessToken

from hive_mcp.domain.errors import ValidationError
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.security.principals import claims_for

if TYPE_CHECKING:
    from pathlib import Path

    from hive_mcp.domain.principal import Principal
    from hive_mcp.security.directory import UserDirectory

MAX_RATE_PER_MIN = 100_000
TOKEN_PREFIX = "hive_"  # noqa: S105 - prefix, not a secret; lets secret scanners recognise leaked tokens


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class TokenRecord:
    token_hash: str
    subject: str
    claims: dict[str, object]
    expires_at: int | None = None  # unix seconds
    label: str = ""
    rate_per_min: int | None = None  # this token's own MCP request limit (None: the server default)


class TokenStore:
    """JSON file of token records (see AtomicJsonFile for the atomic write / cache rules)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = AtomicJsonFile(path, default=[])

    def _read(self) -> list[TokenRecord]:
        return [TokenRecord(**r) for r in self._file.read()]

    def _write(self, records: list[TokenRecord]) -> None:
        self._file.write([asdict(r) for r in records])

    def create(
        self,
        principal: Principal,
        *,
        label: str = "",
        expires_in_days: float | None = None,
        rate_per_min: int | None = None,
    ) -> str:
        """Returns the plaintext token: show it once; it cannot be recovered later."""
        if not label or any(r.label == label for r in self._read()):
            raise ValidationError("a unique label is required (it is how tokens are revoked)")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)  # pragma: no mutate - 32 is the default
        expires = int(time.time() + expires_in_days * 86_400) if expires_in_days else None
        if rate_per_min is not None and not 1 <= rate_per_min <= MAX_RATE_PER_MIN:
            raise ValidationError(f"rate per minute must be 1-{MAX_RATE_PER_MIN}")
        record = TokenRecord(
            hash_token(token), principal.id, claims_for(principal, label), expires, label,
            rate_per_min,
        )  # fmt: skip
        self._write([*self._read(), record])
        return token

    def revoke(self, label: str) -> bool:
        records = self._read()
        kept = [r for r in records if r.label != label]
        if len(kept) == len(records):
            return False
        self._write(kept)
        return True

    def list(self) -> list[TokenRecord]:
        return list(self._read())

    def rate_for_key(self, key: str) -> int | None:
        """The own limit of the token behind a rate-limit key ('tok:' + first 32 hash digits)."""
        prefix = key.removeprefix("tok:")
        for record in self._read():
            if record.rate_per_min and record.token_hash.startswith(prefix):
                return record.rate_per_min
        return None

    def find(self, token: str) -> TokenRecord | None:
        digest = hash_token(token)
        match = None
        for (
            record
        ) in self._read():  # compare every record: no early exit that leaks which hash matched
            if hmac.compare_digest(record.token_hash, digest):
                match = record
        return match


class StaticTokenVerifier:
    """Implements the SDK's TokenVerifier protocol over a TokenStore."""

    def __init__(
        self, store: TokenStore, resource: str | None = None, directory: UserDirectory | None = None
    ) -> None:
        self._store, self._resource, self._directory = store, resource, directory

    async def verify_token(self, token: str) -> AccessToken | None:
        ours = token.startswith(TOKEN_PREFIX)  # pragma: no mutate - a cheap pre-check only
        record = self._store.find(token) if ours else None  # pragma: no mutate
        if record is None or (record.expires_at is not None and record.expires_at <= time.time()):
            return None
        if self._directory and not self._directory.permits(record.subject):
            return None  # the person was removed or disabled: their tokens go with them
        return AccessToken(
            token=token,
            client_id=f"static:{record.label}",
            scopes=["hive"],
            expires_at=record.expires_at,
            resource=self._resource,
            subject=record.subject,
            claims=dict(record.claims),
        )
