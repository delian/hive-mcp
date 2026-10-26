"""Admin sessions live on the server, so signing out, or an admin ending someone's sessions, really
ends them (a stolen cookie is useless afterwards). The cookie only carries a random session id,
signed so that garbage never reaches the store. What a session may do is looked up on every
request from the directory or the token file, so removing a person or revoking a token also
ends their sessions."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from hive_mcp.ports.kv import KeyValueStore

COOKIE_NAME = "hive_admin"
SESSION_SECONDS = 8 * 3600
NS_SESSION = "admin:session"
NS_EPOCH = "admin:epoch"


@dataclass(frozen=True, slots=True)
class AdminSession:
    kind: str  # "user" (Google sign-in, subject = email) or "token" (subject = label#hash)
    subject: str
    csrf: str
    expires: int


class SessionCodec:
    """Signs session ids (domain-separated from the other cookies made with the same key)."""

    def __init__(self, key: bytes) -> None:
        self._key = hmac.new(key, b"hive-admin-session", hashlib.sha256).digest()

    def _mac(self, text: str) -> str:
        return hmac.new(self._key, text.encode(), hashlib.sha256).hexdigest()

    def sign(self, session_id: str) -> str:
        return f"{session_id}.{self._mac(session_id)}"

    def verify(self, cookie: str | None) -> str | None:
        """The session id when the cookie is one of ours, else None."""
        session_id, _, mac = (cookie or "").partition(".")
        if not session_id or not hmac.compare_digest(mac.encode(), self._mac(session_id).encode()):
            return None
        return session_id


def _key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()  # a leaked store cannot be replayed as cookies


class SessionStore:
    def __init__(
        self,
        codec: SessionCodec,
        get_kv: Callable[[], KeyValueStore],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._codec, self._kv, self._clock = codec, get_kv, clock

    async def _epoch(self, subject: str) -> int:
        record = await self._kv().get(NS_EPOCH, _key(subject))
        return int(record["n"]) if record else 0

    async def create(self, kind: str, subject: str) -> str:
        """Start a session; returns the cookie value."""
        session_id = secrets.token_urlsafe(32)
        record = {
            "kind": kind, "subject": subject, "csrf": secrets.token_urlsafe(24),
            "exp": int(self._clock() + SESSION_SECONDS), "epoch": await self._epoch(subject),
        }  # fmt: skip
        await self._kv().put(NS_SESSION, _key(session_id), record, ttl_seconds=SESSION_SECONDS)
        return self._codec.sign(session_id)

    async def load(self, cookie: str | None) -> AdminSession | None:
        session_id = self._codec.verify(cookie)
        if session_id is None:
            return None
        record = await self._kv().get(NS_SESSION, _key(session_id))
        if record is None or int(record["exp"]) <= self._clock():
            return None
        if int(record["epoch"]) != await self._epoch(str(record["subject"])):
            return None  # "sign out everywhere" happened after this session began
        return AdminSession(
            str(record["kind"]), str(record["subject"]), str(record["csrf"]), int(record["exp"])
        )

    async def destroy(self, cookie: str | None) -> None:
        session_id = self._codec.verify(cookie)
        if session_id:
            await self._kv().delete(NS_SESSION, _key(session_id))

    async def revoke_subject(self, subject: str) -> None:
        """End every session of this subject, including ones started later with an older view."""
        await self._kv().put(NS_EPOCH, _key(subject), {"n": await self._epoch(subject) + 1})
