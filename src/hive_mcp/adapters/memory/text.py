"""In-memory TextIndex: BM25 over Unicode-tokenised text."""

from __future__ import annotations

import math
from collections import Counter
from typing import TYPE_CHECKING

from hive_mcp.domain.item import Hit
from hive_mcp.search.tokenize import query_terms, tokenize

if TYPE_CHECKING:
    from hive_mcp.domain.access import Acl, ScopeFilter
    from hive_mcp.ports.indexes import TextDoc

K1, B = 1.2, 0.75


class MemoryText:
    def __init__(self) -> None:
        self._docs: dict[str, tuple[Acl, Counter[str], int]] = {}

    async def upsert(self, doc: TextDoc) -> None:
        tokens = tokenize(doc.text)
        self._docs[doc.item_id] = (doc.acl, Counter(tokens), len(tokens))

    async def search(self, flt: ScopeFilter, query: str, limit: int) -> list[Hit]:
        terms = set(query_terms(query))
        if not terms or limit < 1:
            return []
        total = len(self._docs)
        avg_len = sum(n for _, _, n in self._docs.values()) / total if total else 1.0
        df = {t: sum(1 for _, tf, _ in self._docs.values() if t in tf) for t in terms}
        hits = []
        for item_id, (acl, tf, length) in self._docs.items():
            if flt.tier(acl) is None:
                continue
            score = 0.0
            for term in terms:
                if tf[term] == 0:
                    continue
                idf = math.log(1 + (total - df[term] + 0.5) / (df[term] + 0.5))
                norm = tf[term] + K1 * (1 - B + B * length / avg_len)
                score += idf * tf[term] * (K1 + 1) / norm
            if score > 0:
                hits.append(Hit(item_id, score))
        hits.sort(key=lambda h: (-h.score, h.item_id))
        return hits[:limit]

    async def update_acl(self, item_id: str, acl: Acl) -> None:
        if item_id in self._docs:
            _, tf, length = self._docs[item_id]
            self._docs[item_id] = (acl, tf, length)

    async def delete(self, item_id: str) -> None:
        self._docs.pop(item_id, None)
