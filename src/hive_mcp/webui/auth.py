"""Who a session belongs to, decided afresh on each request from the directory or the token file."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import HiveError
from hive_mcp.security.principals import principal_from_claims
from hive_mcp.security.tokens import TOKEN_PREFIX

if TYPE_CHECKING:
    from hive_mcp.domain.principal import Principal
    from hive_mcp.security.directory import UserDirectory
    from hive_mcp.security.tokens import TokenRecord, TokenStore
    from hive_mcp.webui.session import AdminSession


def session_subject(record: TokenRecord) -> str:
    return f"{record.label}#{record.token_hash[:16]}"


class AdminAuth:
    def __init__(self, directory: UserDirectory, tokens: TokenStore) -> None:
        self._directory, self._tokens = directory, tokens

    def token_label(self, token: str) -> str | None:
        """The label of a valid static token (what the session will hold), else None."""
        record = self._tokens.find(token) if token.startswith(TOKEN_PREFIX) else None
        if record is None or not record.label or self._expired(record.expires_at):
            return None
        return session_subject(record)

    @staticmethod
    def _expired(expires_at: int | None) -> bool:
        return expires_at is not None and expires_at <= time.time()

    def principal(self, session: AdminSession) -> Principal | None:
        if session.kind == "user":
            user = self._directory.lookup(session.subject)  # None when unknown or disabled
            return user.principal() if user else None
        return self.principal_for_label(session.subject)

    def principal_for_label(self, subject: str) -> Principal | None:
        """`subject` names the token by label *and* hash: a revoked token whose label is reused
        for a new token does not revive the old session."""
        for record in self._tokens.list():
            if session_subject(record) == subject and not self._expired(record.expires_at):
                if not self._directory.permits(record.subject):
                    return None
                try:
                    return principal_from_claims(record.subject, record.claims)
                except HiveError:
                    return None
        return None
