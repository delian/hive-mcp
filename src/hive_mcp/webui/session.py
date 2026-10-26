"""Signed, expiring admin session cookie. It names who signed in; what they may do is looked up on
every request, so revoking a token or disabling a user ends the session at once."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

COOKIE_NAME = "hive_admin"
SESSION_SECONDS = 8 * 3600


@dataclass(frozen=True, slots=True)
class AdminSession:
    kind: str  # "user" (Google sign-in, subject = email) or "token" (subject = token label)
    subject: str
    csrf: str
    expires: int


class SessionCodec:
    def __init__(self, key: bytes) -> None:
        self._key = hmac.new(
            key, b"hive-admin-session", hashlib.sha256
        ).digest()  # domain separation

    def _mac(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self, kind: str, subject: str, now: float | None = None) -> str:
        expires = int((time.time() if now is None else now) + SESSION_SECONDS)
        data = {"k": kind, "s": subject, "c": secrets.token_urlsafe(24), "e": expires}
        payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
        return f"{payload}.{self._mac(payload)}"

    def read(self, cookie: str | None, now: float | None = None) -> AdminSession | None:
        payload, _, mac = (cookie or "").partition(".")
        if not payload or not hmac.compare_digest(mac.encode(), self._mac(payload).encode()):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload))
            session = AdminSession(str(data["k"]), str(data["s"]), str(data["c"]), int(data["e"]))
        except (ValueError, KeyError, TypeError):
            return None
        if session.expires <= (time.time() if now is None else now):
            return None
        return session
