"""Score keyword / vector / hybrid retrieval at several corpus sizes (docs/EVALUATION.md, "Scale").

    HIVE_EMBEDDER=onnx HIVE_MODELS_DIR=models uv run python scripts/eval_scale.py scale.json

`scale.json` comes from scripts/eval_scale_data.py. The documents are embedded once (cached in
`--cache`), indexed in the in-memory text and vector indexes (BM25 and exact cosine, as `hive eval`
does), and the top-50 hits of every query are kept; each ranking variant is then a pure function of
those two hit lists, so trying a fusion setting costs nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pickle
import tempfile
from collections import defaultdict
from pathlib import Path

import numpy as np

from hive_mcp.adapters.memory.text import MemoryText
from hive_mcp.adapters.memory.vectors import MemoryVectors
from hive_mcp.config import Settings
from hive_mcp.domain.access import read_filter
from hive_mcp.domain.item import Item
from hive_mcp.domain.principal import Principal
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.embeddings.factory import make_embedder
from hive_mcp.evaluation.metrics import summarise
from hive_mcp.ports.indexes import TextDoc
from hive_mcp.search.ranking import RankingConfig, fuse
from hive_mcp.services.indexing import embedding_texts

OWNER = Principal("eval", "eval", projects=frozenset({"eval"}))
FLOOR = 0.58  # bge-small's relevance floor
POOL = 50  # what ConsultService asks each index for at limit 10
BATCH = 1000
Hits = list[tuple[str, float]]


def items_of(docs: list[dict]) -> list[Item]:
    scope, now = (
        Scope(ScopeLevel.PROJECT, "eval"),
        __import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").UTC),
    )
    return [
        Item(id=d["id"], org_id="eval", kind="note", title=d["title"], body=d["body"], scope=scope,  # type: ignore[arg-type]
             author_id="eval", created_at=now, updated_at=now)
        for d in docs
    ]  # fmt: skip


async def embed_all(
    items: list[Item], queries: list[str], cache: Path
) -> tuple[list[np.ndarray], np.ndarray]:
    cache.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - a one-off script
    embedder = make_embedder(Settings())
    per_doc: list[np.ndarray] = []
    for start in range(0, len(items), BATCH):
        shard = cache / f"{embedder.model_id}-{start}-{len(items[start : start + BATCH])}.npz"
        if not shard.exists():
            texts, counts = [], []
            for it in items[start : start + BATCH]:
                parts = embedding_texts(it)
                texts += parts
                counts.append(len(parts))
            mat = np.vstack(await embedder.embed(texts))
            np.savez(shard, mat=mat, counts=np.array(counts))
            print(f"embedded docs {start}-{start + len(counts)}", flush=True)
        z = np.load(shard)
        mat = z["mat"]  # NpzFile re-reads the array on every access
        offsets = np.concatenate([[0], np.cumsum(z["counts"])])
        per_doc += [mat[offsets[i] : offsets[i + 1]] for i in range(len(offsets) - 1)]
    qpath = cache / f"{embedder.model_id}-queries-{len(queries)}.npy"
    if not qpath.exists():
        np.save(qpath, np.vstack([await embedder.embed_query(q) for q in queries]))
    return per_doc, np.load(qpath)


async def hits_for(items: list[Item], vecs: list[np.ndarray], qvecs: np.ndarray, queries: list[str],
                   model_id: str) -> tuple[list[Hits], list[Hits]]:  # fmt: skip
    text, vectors, flt = MemoryText(), MemoryVectors(), read_filter(OWNER)
    for it, v in zip(items, vecs, strict=True):
        await text.upsert(TextDoc(it.id, it.acl, f"{it.title}\n{it.body}\n"))
        await vectors.replace(it.id, model_id, it.acl, list(v))
    kw, vec = [], []
    for q, qv in zip(queries, qvecs, strict=True):
        kw.append([(h.item_id, h.score) for h in await text.search(flt, q, POOL)])
        vec.append([(h.item_id, h.score) for h in await vectors.search(flt, model_id, qv, POOL)])
    return kw, vec


def hybrid(  # noqa: ANN201
    vector_weight: float = 2.0, depth: int = 5, floor: float = FLOOR, lead: float = 0.0
):
    """The production fusion (`search.ranking.fuse`) with one setting changed."""
    cfg = RankingConfig(vector_weight=vector_weight, keyword_depth=depth, keyword_lead_ratio=lead)

    def run(kw: Hits, vec: Hits) -> list[str]:
        fused = fuse([i for i, _ in kw], [i for i, s in vec if s >= floor], cfg, [s for _, s in kw])
        return sorted(fused, key=lambda i: (-fused[i], i))

    return run


VARIANTS = {
    "keyword": lambda kw, vec: [i for i, _ in kw],
    "vector (floor .58)": lambda kw, vec: [i for i, s in vec if s >= FLOOR],
    "hybrid default (w2, top5, .58)": hybrid(),
    "hybrid w1": hybrid(1.0),
    "hybrid w3": hybrid(3.0),
    "hybrid top3": hybrid(depth=3),
    "hybrid top20": hybrid(depth=20),
    "hybrid floor .65": hybrid(floor=0.65),
    "hybrid floor .70": hybrid(floor=0.70),
    "hybrid floor 0": hybrid(floor=0.0),
    "hybrid lead 1.5": hybrid(lead=1.5),
    "hybrid lead 2.0": hybrid(lead=2.0),
    "hybrid lead 3.0": hybrid(lead=3.0),
}


def score(ranked: list[list[str]], queries: list[dict]) -> dict[str, dict[str, float]]:
    by: dict[str, list[int]] = defaultdict(list)
    for n, q in enumerate(queries):
        by[q["type"]].append(n)
    out = {}
    for kind, idx in by.items():
        if kind == "negative":
            out[kind] = {"answered": sum(1 for n in idx if ranked[n]) / len(idx), "n": len(idx)}
            continue
        s = summarise([(ranked[n], frozenset(queries[n]["relevant"])) for n in idx])
        out[kind] = {
            "R1": s.recall[1],
            "R3": s.recall[3],
            "R10": s.recall[10],
            "MRR": s.mrr,
            "n": s.queries,
        }
    pos = [v for k, v in out.items() if k != "negative"]
    out["macro"] = {m: float(np.mean([p[m] for p in pos])) for m in ("R1", "R3", "R10", "MRR")}
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("data", type=Path)
    ap.add_argument("--cache", type=Path, default=Path(tempfile.gettempdir()) / "eval_scale_cache")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--limit", type=int, default=0, help="only sizes up to this many documents")
    ap.add_argument(
        "--embed-upto", type=int, default=0, help="only embed this many documents, then stop"
    )
    args = ap.parse_args()
    data = json.loads(args.data.read_text())
    items = items_of(data["docs"])
    queries = data["queries"]
    if (
        args.embed_upto
    ):  # the ONNX runtime's memory grows with every new input shape: embed in slices
        await embed_all(items[: args.embed_upto], [q["q"] for q in queries], args.cache)
        return
    model_id = make_embedder(Settings()).model_id
    results: dict[str, dict] = {}
    for size in sorted(
        n for n in data["sizes"] if not args.limit or n <= args.limit
    ):  # small sizes first: each one only embeds its own documents
        per_doc, qvecs = await embed_all(items[:size], [q["q"] for q in queries], args.cache)
        hp = args.cache / f"hits-{model_id}-{size}-{len(queries)}.pkl"
        if hp.exists():
            kw, vec = pickle.loads(hp.read_bytes())  # noqa: S301 - our own cache file
        else:
            kw, vec = await hits_for(
                items[:size], per_doc[:size], qvecs, [q["q"] for q in queries], model_id
            )
            hp.write_bytes(pickle.dumps((kw, vec)))
        print(f"size {size}", flush=True)
        results[str(size)] = {
            name: score([fn(k, v) for k, v in zip(kw, vec, strict=True)], queries)
            for name, fn in VARIANTS.items()
        }
        if args.out:  # written after every size so partial runs are usable
            args.out.write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
