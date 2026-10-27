"""Who is this Slack user? A person in the user directory, found through their Slack email.

Slack confirms email addresses of workspace members, so the address names the person; the
directory then decides what they may do, exactly as for every other front door (a person who is
not in the directory, or is disabled, gets nothing). Guests and bots are refused. Addresses are
looked up with the bot token (scope `users:read.email`) and cached for an hour; `HIVE_SLACK_USERS`
can pin Slack ids to addresses instead, for installs that do not want a bot token.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from hive_mcp.security.directory import normalize_email

if TYPE_CHECKING:
    from collections.abc import Callable

    from hive_mcp.domain.principal import Principal
    from hive_mcp.ports.kv import KeyValueStore
    from hive_mcp.security.directory import UserDirectory

log = logging.getLogger("hive.slack")
CACHE_SECONDS = 3600
USERS_INFO = "https://slack.com/api/users.info"
EMAIL_NS = "slack:email"


class SlackIdentity:
    def __init__(  # noqa: PLR0913 - collaborators
        self,
        directory: UserDirectory,
        get_kv: Callable[[], KeyValueStore],
        *,
        team_id: str,
        bot_token: str | None = None,
        pinned: dict[str, str] | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._directory, self._get_kv, self._team = directory, get_kv, team_id
        self._token, self._pinned = bot_token, pinned or {}
        self._http = http or httpx.AsyncClient(timeout=5.0)

    async def resolve(self, team_id: str, user_id: str) -> Principal | None:
        if team_id != self._team or not user_id.isalnum():
            return None
        email = self._pinned.get(user_id) or await self._email(user_id)
        if not email:
            return None
        try:
            user = self._directory.lookup(normalize_email(email))
        except Exception:
            return None
        return user.principal() if user else None

    async def _email(self, user_id: str) -> str | None:
        if not self._token:
            return None
        key = f"{self._team}:{user_id}"
        cached = await self._get_kv().get(EMAIL_NS, key)
        if cached:
            return str(cached["email"]) or None
        email = await self._lookup(user_id)
        await self._get_kv().put(EMAIL_NS, key, {"email": email or ""}, ttl_seconds=CACHE_SECONDS)
        return email

    async def _lookup(self, user_id: str) -> str | None:
        try:
            response = await self._http.get(
                USERS_INFO,
                params={"user": user_id},
                headers={"Authorization": f"Bearer {self._token}"},
            )
            body = response.json()
        except (httpx.HTTPError, ValueError):
            log.warning("slack users.info unreachable")
            return None
        user = body.get("user") if body.get("ok") else None
        if not user or user.get("deleted") or user.get("is_bot"):
            return None
        if user.get("is_restricted") or user.get("is_ultra_restricted"):
            return None  # guests are not members of the organisation
        email = (user.get("profile") or {}).get("email")
        return email if isinstance(email, str) else None
