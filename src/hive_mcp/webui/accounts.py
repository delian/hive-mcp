"""Managing people and tokens from the admin page, with the guards a mouse click needs: an admin
cannot lock themselves (or the last admin) out, cannot touch another organisation, and every
change is written to the audit trail."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from hive_mcp.domain.clock import SystemClock
from hive_mcp.domain.errors import NotFound, PermissionDenied, ValidationError
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import parse_scope, scope_key
from hive_mcp.ports.audit import AuditEvent
from hive_mcp.security.directory import DirectoryUser, normalize_email

if TYPE_CHECKING:
    from collections.abc import Callable

    from hive_mcp.ports.audit import AuditLog
    from hive_mcp.security.directory import UserDirectory
    from hive_mcp.security.tokens import TokenRecord, TokenStore
    from hive_mcp.webui.session import SessionStore

log = logging.getLogger("hive.security")
MAX_TOKEN_DAYS = 3650


def split(text: str) -> frozenset[str]:
    """'a, b  c' -> {'a', 'b', 'c'}"""
    return frozenset(part for part in text.replace(",", " ").split() if part)


def parse_role(name: str) -> Role:
    try:
        return Role[name.strip().upper()]
    except KeyError:
        raise ValidationError(f"unknown role {name!r} (viewer, member, editor, admin)") from None


class AccountAdmin:
    def __init__(
        self,
        directory: UserDirectory,
        tokens: TokenStore,
        sessions: SessionStore,
        audit: Callable[[], AuditLog],
    ) -> None:
        self._directory, self._tokens, self._sessions, self._audit = (
            directory, tokens, sessions, audit,
        )  # fmt: skip

    # ------------------------------------------------------------------ helpers
    async def _log(self, actor: Principal, action: str, target: str, **details: str) -> None:
        event = AuditEvent(actor.org_id, actor.id, action, SystemClock().now(), target, details)
        await self._audit().append(event)
        log.info("%s actor=%s target=%s", action, actor.id, target)

    def _mine(self, actor: Principal, email: str) -> DirectoryUser:
        user = self._find(normalize_email(email))
        if user is None or user.org_id != actor.org_id:
            raise NotFound(email)  # another organisation's people do not exist here
        return user

    def _find(self, email: str) -> DirectoryUser | None:
        return next((u for u in self._directory.list() if u.email == email), None)  # incl. disabled

    def _admins_left(self, actor: Principal, without: str, changed: DirectoryUser | None) -> int:
        """Enabled admins of the organisation after the change (`changed` replaces `without`)."""
        users = [
            u for u in self._directory.list() if u.org_id == actor.org_id and u.email != without
        ]
        users += [changed] if changed else []
        return sum(1 for u in users if u.role is Role.ADMIN and not u.disabled)

    def _guard(self, actor: Principal, target: DirectoryUser, after: DirectoryUser | None) -> None:
        """`after` is None when the user is removed."""
        weakened = after is None or after.disabled or after.role < Role.ADMIN
        if actor.id == target.email and weakened:
            raise PermissionDenied("you cannot remove, disable or demote yourself")
        if (
            target.role is Role.ADMIN
            and weakened
            and self._admins_left(actor, target.email, after) < 1
        ):
            raise PermissionDenied("this would leave the organisation without an admin")

    # ------------------------------------------------------------------ people
    def users(self, actor: Principal) -> list[DirectoryUser]:
        return [u for u in self._directory.list() if u.org_id == actor.org_id]

    async def save_user(  # noqa: PLR0913, PLR0917 - the fields of a person
        self, actor: Principal, email: str, role: str, teams: str, projects: str, approves: str,
        *, disabled: bool = False, reset_identity: bool = False,
    ) -> DirectoryUser:  # fmt: skip
        address = normalize_email(email)
        existing = self._find(address)
        if existing and existing.org_id != actor.org_id:
            raise ValidationError(f"{address} belongs to another organisation")
        scopes = frozenset(scope_key(parse_scope(k)) for k in split(approves))
        user = DirectoryUser(
            address, actor.org_id, parse_role(role), split(teams), split(projects), disabled, scopes,
            existing.google_sub if existing and not reset_identity else None,
        )  # fmt: skip
        if existing:
            self._guard(actor, existing, user)
        self._directory.upsert(user)
        await self._log(
            actor, "admin.user.save", address, role=role, new="no" if existing else "yes"
        )
        return user

    async def set_disabled(self, actor: Principal, email: str, disabled: bool) -> DirectoryUser:
        target = self._mine(actor, email)
        after = replace(target, disabled=disabled)
        self._guard(actor, target, after)
        self._directory.upsert(after)
        if disabled:
            await self._sessions.revoke_subject(target.email)
        await self._log(
            actor, "admin.user.disable" if disabled else "admin.user.enable", target.email
        )
        return after

    async def remove_user(self, actor: Principal, email: str) -> None:
        target = self._mine(actor, email)
        self._guard(actor, target, None)
        self._directory.remove(target.email)
        await self._sessions.revoke_subject(target.email)
        await self._log(actor, "admin.user.remove", target.email)

    async def sign_out_everywhere(self, actor: Principal, email: str) -> None:
        target = self._mine(actor, email)
        await self._sessions.revoke_subject(target.email)
        await self._log(actor, "admin.user.signout", target.email)

    # ------------------------------------------------------------------ tokens
    def tokens(self, actor: Principal) -> list[TokenRecord]:
        return [t for t in self._tokens.list() if t.claims.get("hive_org") == actor.org_id]

    async def create_token(  # noqa: PLR0913, PLR0917 - the fields of a token
        self, actor: Principal, label: str, subject: str, role: str, teams: str, projects: str,
        approves: str, days: str, rate: str = "",
    ) -> str:  # fmt: skip
        """The plaintext token: it is shown once and cannot be recovered."""
        who = subject.strip()
        if not who or len(who) > 200:  # noqa: PLR2004
            raise ValidationError("a subject (who the token acts as) is required")
        scopes = frozenset(parse_scope(k) for k in split(approves))
        expires = None
        if days.strip():
            try:
                expires = float(days)
            except ValueError:
                raise ValidationError("days must be a number") from None
            if not 0 < expires <= MAX_TOKEN_DAYS:
                raise ValidationError(f"days must be between 0 and {MAX_TOKEN_DAYS}")
        per_minute = None
        if rate.strip():
            try:
                per_minute = int(rate)
            except ValueError:
                raise ValidationError("the rate limit must be a whole number per minute") from None
        principal = Principal(
            who, actor.org_id, parse_role(role), split(teams), split(projects), scopes
        )
        token = self._tokens.create(
            principal, label=label.strip(), expires_in_days=expires, rate_per_min=per_minute
        )
        await self._log(actor, "admin.token.create", label.strip(), subject=who, role=role)
        return token

    async def revoke_token(self, actor: Principal, label: str) -> None:
        if not any(t.label == label for t in self.tokens(actor)):
            raise NotFound(label)
        self._tokens.revoke(label)
        await self._log(actor, "admin.token.revoke", label)
