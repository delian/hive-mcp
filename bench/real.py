"""Real-data corpus: Simple English Wikipedia chunks embedded with a local bge-small ONNX model.

`prepare N` extracts chunks + groups (group = hash of the article's first category, so groups are
topic-correlated like real projects; 20% are reassigned at random). `embed` writes vectors in shards.
"""

from __future__ import annotations

import bz2
import re
import sys
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

from bench.common import DIM, GROUPS, REAL_DIR

DUMP = REAL_DIR / "simplewiki.xml.bz2"
WORDS_PER_CHUNK = 110
MAX_PER_ARTICLE = 5
_CAT = re.compile(r"\[\[Category:([^\]|]+)", re.I)


def _strip_nested(text: str, open_: str, close: str) -> str:
    pat = re.compile(
        re.escape(open_)
        + r"[^"
        + re.escape(open_[0])
        + re.escape(close[0])
        + r"]*"
        + re.escape(close)
    )
    prev = None
    while prev != text:
        prev, text = text, pat.sub("", text)
    return text


def clean(wikitext: str) -> str:
    t = re.sub(r"<!--.*?-->", "", wikitext, flags=re.S)
    t = re.sub(r"<ref[^>]*?/>|<ref.*?</ref>", "", t, flags=re.S)
    t = _strip_nested(t, "{{", "}}")
    t = _strip_nested(t, "{|", "|}")
    t = re.sub(
        r"\[\[(?:File|Image|Category):[^\]]*(?:\[\[[^\]]*\]\][^\]]*)*\]\]", "", t, flags=re.I
    )
    t = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"\[\[([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"\[https?://\S+ ([^\]]*)\]|\[https?://\S+\]", r"\1", t)
    t = re.sub(r"<[^>]+>|'{2,}|^[=*#:;]+|={2,}", " ", t, flags=re.M)
    return re.sub(r"\s+", " ", t).strip()


def articles():  # noqa: ANN201
    with bz2.open(DUMP, "rb") as f:
        for _, el in ET.iterparse(f):
            if not el.tag.endswith("}page"):
                continue
            ns = el.find("./{*}ns")
            title = el.findtext("./{*}title") or ""
            text = el.findtext("./{*}revision/{*}text") or ""
            if ns is not None and ns.text == "0" and not text.lower().startswith("#redirect"):
                yield title, text
            el.clear()


def prepare(n: int) -> None:
    rng = np.random.default_rng(3)
    texts, groups, chunk_raw = [], [], []
    for title, wt in articles():
        cats = _CAT.findall(wt)
        home = zlib.crc32(cats[0].encode()) % GROUPS if cats else int(rng.integers(GROUPS))
        words = clean(wt).split()
        for k in range(MAX_PER_ARTICLE):
            piece = words[k * WORDS_PER_CHUNK : (k + 1) * WORDS_PER_CHUNK]
            if len(piece) < 50:
                break
            raw = f"{title}. " + " ".join(piece)
            chunk_raw.append(raw)
            texts.append(" ".join(re.findall(r"[a-z0-9]+", raw.lower())))
            groups.append(home if rng.random() < 0.8 else int(rng.integers(GROUPS)))
        if len(texts) >= n:
            break
    n = min(n, len(texts))
    REAL_DIR.mkdir(parents=True, exist_ok=True)
    (REAL_DIR / "texts.txt").write_text("\n".join(texts[:n]))
    (REAL_DIR / "raw.txt").write_text("\n".join(chunk_raw[:n]))
    np.save(REAL_DIR / "group.npy", np.array(groups[:n], dtype=np.int32))
    print("prepared", n, "chunks; mean words", np.mean([len(t.split()) for t in texts[:n]]))


def embed(shard: int = 5000) -> None:
    from bench.embed import Embedder

    raw = (REAL_DIR / "raw.txt").read_text().split("\n")
    out = REAL_DIR / "shards"
    out.mkdir(exist_ok=True)
    e = Embedder()
    for lo in range(0, len(raw), shard):
        p = out / f"{lo:08d}.npy"
        if p.exists():
            continue
        parts = [e.encode(raw[i : i + 64]) for i in range(lo, min(lo + shard, len(raw)), 64)]
        np.save(p, np.concatenate(parts).astype(np.float16))
        print("embedded", lo + shard, flush=True)


if __name__ == "__main__":
    prepare(int(sys.argv[2])) if sys.argv[1] == "prepare" else embed()
    assert DIM == 384 and Path(DUMP).exists()
