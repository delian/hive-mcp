"""What the search-feedback trail says about the knowledge base: gaps, winners and losers.

Searches that returned nothing, or whose results nobody opened or rated useful, are questions the
knowledge base failed to answer: the best list of notes still to be written.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hive_mcp.evaluation.feedback import pair_searches_with_opens
from hive_mcp.services.feedback import QUERY

if TYPE_CHECKING:
    from collections.abc import Iterable

    from hive_mcp.ports.audit import AuditRecord

TOP = 10


@dataclass
class FeedbackReport:
    searches: int = 0
    answered: int = 0  # searches whose text got a result that was opened or rated useful
    gaps: list[tuple[str, int]] = field(default_factory=list)  # (text, times) nothing helped
    empty: list[tuple[str, int]] = field(default_factory=list)  # (text, times) nothing was found
    useful: list[tuple[str, int]] = field(default_factory=list)  # (item id, votes)
    useless: list[tuple[str, int]] = field(default_factory=list)


def build_report(records: Iterable[AuditRecord]) -> FeedbackReport:
    rows = list(records)
    pairs = pair_searches_with_opens(rows)
    searched: Counter[str] = Counter()
    empty: Counter[str] = Counter()
    for r in rows:
        if r.event.action == QUERY and r.event.details.get("q"):
            text = r.event.details["q"]
            searched[text] += 1
            if not r.event.details.get("hits"):
                empty[text] += 1
    gaps = Counter(
        {q: n for q, n in searched.items() if q not in pairs.relevant and q not in empty}
    )
    votes: Counter[str] = Counter(i for ids in pairs.relevant.values() for i in ids)
    bad: Counter[str] = Counter(i for ids in pairs.irrelevant.values() for i in ids)
    return FeedbackReport(
        searches=pairs.searches,
        answered=sum(n for q, n in searched.items() if q in pairs.relevant),
        gaps=gaps.most_common(TOP),
        empty=empty.most_common(TOP),
        useful=votes.most_common(TOP),
        useless=bad.most_common(TOP),
    )
