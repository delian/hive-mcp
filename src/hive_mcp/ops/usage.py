"""Who uses the server, and how much: tool calls counted per person, saved as audit events.

Calls are counted in memory (a dict increment per call) and written out every few minutes as one
`usage.tools` audit event per person, with counts only, never content. The audit trail is shared
by all replicas and survives restarts, so the activity report simply adds events up. A crash loses
at most the last interval's counts.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING

from mcp.server.auth.middleware.auth_context import get_access_token

from hive_mcp.ports.audit import AuditEvent
from hive_mcp.security.principals import principal_from_claims

if TYPE_CHECKING:
    from hive_mcp.domain.clock import Clock
    from hive_mcp.ports.audit import AuditLog

log = logging.getLogger("hive.usage")
ACTION = "usage.tools"
FLUSH_SECONDS = 300
MAX_TOOLS_LISTED = 12


class UsageMeter:
    def __init__(self) -> None:
        self._counts: dict[tuple[str, str], Counter[str]] = {}

    def record(self, org_id: str, user_id: str, tool: str) -> None:
        self._counts.setdefault((org_id, user_id), Counter())[tool] += 1

    def record_current(self, tool: str) -> None:
        """The caller of the running tool call, from the verified token. Never raises."""
        try:
            token = get_access_token()
            if token is not None:
                who = principal_from_claims(token.subject, token.claims)
                self.record(who.org_id, who.id, tool)
        except Exception:
            log.debug("usage not recorded", exc_info=True)

    async def flush(self, audit: AuditLog, clock: Clock) -> int:
        """Write the counts as events and start over; failed writes are kept for the next round."""
        pending, self._counts = self._counts, {}
        written = 0
        for (org, user), tools in pending.items():
            top = ",".join(f"{t}={n}" for t, n in tools.most_common(MAX_TOOLS_LISTED))
            event = AuditEvent(
                org, user, ACTION, clock.now(), None, {"calls": str(tools.total()), "tools": top}
            )
            try:
                await audit.append(event)
                written += 1
            except Exception:
                log.exception("usage flush failed; keeping the counts")
                bucket = self._counts.setdefault((org, user), Counter())
                bucket.update(tools)
        return written


METER = UsageMeter()
