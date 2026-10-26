"""Retrieval evaluation sets: documents plus queries with the documents that should be found."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import ValidationError

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class EvalDoc:
    id: str
    title: str
    body: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvalQuery:
    text: str
    relevant: frozenset[str]  # empty = nothing in the corpus should be returned ("negative")
    kind: str = "query"  # free label used to break results down: paraphrase, keyword, negative...


@dataclass(frozen=True, slots=True)
class Dataset:
    name: str
    docs: tuple[EvalDoc, ...]
    queries: tuple[EvalQuery, ...]


def parse(raw: object) -> Dataset:
    """Validate decoded JSON: {name, docs: [...], queries: [{q, relevant, type?}]}."""
    if not isinstance(raw, dict) or not raw.get("docs") or not raw.get("queries"):
        raise ValidationError("an evaluation set needs non-empty 'docs' and 'queries'")
    try:
        docs = tuple(
            EvalDoc(str(d["id"]), str(d["title"]), str(d["body"]), tuple(d.get("tags", ())))
            for d in raw["docs"]
        )
        queries = tuple(
            EvalQuery(
                str(q["q"]), frozenset(map(str, q.get("relevant", ()))), str(q.get("type", "query"))
            )
            for q in raw["queries"]
        )
    except (KeyError, TypeError) as exc:
        raise ValidationError(f"malformed evaluation set: {exc!r}") from exc
    ids = {d.id for d in docs}
    if len(ids) != len(docs):
        raise ValidationError("document ids must be unique")
    unknown = {r for q in queries for r in q.relevant} - ids
    if unknown:
        raise ValidationError(f"queries refer to unknown documents: {sorted(unknown)[:5]}")
    return Dataset(str(raw.get("name", "unnamed")), docs, queries)


def load(path: Path) -> Dataset:
    try:
        return parse(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise ValidationError(f"cannot read evaluation set {path}: {exc}") from exc
