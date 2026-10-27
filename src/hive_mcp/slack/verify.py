"""Slack request signatures: HMAC-SHA256 over `v0:<timestamp>:<raw body>` with the signing secret.

A request older than five minutes is refused, so a captured request cannot be replayed later;
within the window the routes remember each signature once (see routes.py).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

WINDOW_SECONDS = 300


def sign(secret: str, timestamp: str, body: bytes) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def verify(
    secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    now: Callable[[], float] = time.time,
) -> bool:
    # str.isdigit() accepts characters such as '²' that int() then rejects: ASCII only
    if not timestamp or not signature or not (timestamp.isascii() and timestamp.isdigit()):
        return False
    if abs(now() - int(timestamp)) > WINDOW_SECONDS:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body).encode(), signature.encode())
