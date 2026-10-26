"""Optional record of what people search for and then open: real relevance labels, over time.

Off by default (HIVE_SEARCH_FEEDBACK). Search texts can reveal what someone is working on, so
they are redacted, shortened and written to the audit trail, which only admins can read.
`hive eval-build` turns the trail into an evaluation set; nothing else uses it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.ports.audit import AuditEvent
from hive_mcp.security.redact import redact

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hive_mcp.domain.clock import Clock
    from hive_mcp.domain.principal import Principal
    from hive_mcp.ports.audit import AuditLog

QUERY = "search.query"
OPEN = "search.open"
MAX_QUERY_CHARS = 200
MAX_HITS = 25


class SearchFeedback:
    def __init__(self, audit: AuditLog, clock: Clock) -> None:
        self._audit, self._clock = audit, clock

    async def queried(self, who: Principal, query: str, item_ids: Sequence[str]) -> None:
        text = redact(query).text.strip()[:MAX_QUERY_CHARS]
        details = {"q": text, "hits": ",".join(item_ids[:MAX_HITS])}
        await self._audit.append(
            AuditEvent(who.org_id, who.id, QUERY, self._clock.now(), None, details)
        )

    async def opened(self, who: Principal, item_id: str) -> None:
        await self._audit.append(
            AuditEvent(who.org_id, who.id, OPEN, self._clock.now(), item_id, {})
        )
