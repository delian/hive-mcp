"""Turning the search-feedback trail into an evaluation set.

A person who searches and then opens a result a moment later is telling us that result was
relevant to that search. Pairs are weak labels (people also open the wrong thing), so the output
is meant to be reviewed: good for spotting regressions and choosing a floor, not for ground truth.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from hive_mcp.services.feedback import OPEN, QUERY

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import datetime

    from hive_mcp.ports.audit import AuditRecord

WINDOW = timedelta(minutes=30)
KEEP_QUERIES = 10  # recent searches remembered per person


@dataclass
class _Recent:
    text: str
    hits: frozenset[str]
    at: datetime


@dataclass
class Pairs:
    relevant: dict[str, set[str]] = field(default_factory=dict)  # query text -> opened result ids
    searches: int = 0
    opens: int = 0


def pair_searches_with_opens(records: Iterable[AuditRecord], window: timedelta = WINDOW) -> Pairs:
    """Records must be in the order they happened. Each open is credited to the latest search by
    the same person, within `window`, whose results included that item."""
    pairs = Pairs()
    recent: dict[str, list[_Recent]] = {}
    for record in records:
        event = record.event
        if event.action == QUERY and event.details.get("q"):
            pairs.searches += 1
            hits = frozenset(filter(None, event.details.get("hits", "").split(",")))
            queue = recent.setdefault(event.actor_id, [])
            queue.append(_Recent(event.details["q"], hits, event.at))
            del queue[:-KEEP_QUERIES]
        elif event.action == OPEN and event.target_id:
            pairs.opens += 1
            for search in reversed(recent.get(event.actor_id, [])):
                if event.at - search.at <= window and event.target_id in search.hits:
                    pairs.relevant.setdefault(search.text, set()).add(event.target_id)
                    break
    return pairs


def build_set(  # noqa: PLR0913, PLR0917 - the pieces of a set
    name: str,
    pairs: Pairs,
    documents: Mapping[str, tuple[str, str]],
    distractors: Sequence[tuple[str, str, str]] = (),
    negatives: Iterable[str] = (),
    seed: int = 0,
) -> dict[str, Any]:
    """{name, docs, queries} in the format `hive eval` reads. Only items that still exist are used;
    queries whose opened items vanished are dropped."""
    queries = []
    for text, ids in sorted(pairs.relevant.items()):
        alive = sorted(i for i in ids if i in documents)
        if alive:
            queries.append({"q": text, "relevant": alive, "type": "feedback"})
    needed = {i for q in queries for i in q["relevant"]}
    docs = [{"id": i, "title": documents[i][0], "body": documents[i][1]} for i in sorted(needed)]
    pool = [d for d in distractors if d[0] not in needed]
    random.Random(seed).shuffle(pool)  # noqa: S311 - reproducible sampling, not security
    docs += [{"id": i, "title": t, "body": b} for i, t, b in pool]
    queries += [{"q": q, "relevant": [], "type": "negative"} for q in negatives]
    return {"name": name, "docs": docs, "queries": queries}
