"""Build a scale evaluation set from public Stack Exchange text (see docs/EVALUATION.md, "Scale").

    uv run python scripts/eval_scale_data.py OUT.json [--sizes 300,1000,5000,20000] [--seed 7]

Source: the Hugging Face dataset
`flax-sentence-embeddings/stackexchange_title_best_voted_answer_jsonl`
(question title, best answer; CC BY-SA). Technical sites are the corpus, everyday-life sites are the
unrelated ("negative") queries. Only the first few MB of each site file are downloaded.

Corpora are nested (the first N documents of one shuffled list), and every query is derived from the
first `core` documents, so the same queries are asked at every size and only the distractors grow.

Labels are derived, not judged:
- question: the Stack Exchange question title (written by someone else) -> the answer it belongs to.
  The note's title is the answer's first sentence, so title and query share few words.
- heldout: one sentence cut out of the answer (not the first) -> that note, without the sentence.
- identifier: a code-like word of the answer (path, flag, error name) that no other note contains.
- planted / planted-ctx: a unique ticket, error code or host name planted in the note; the query is
  the bare id, or the id plus three words of the note's title.
  For identifier and planted queries the relevant set is every note that contains all query tokens.
"""
# ruff: noqa: PLR0912, PLR2004, S311  - a one-off data script: seeded sampling, ad-hoc thresholds

from __future__ import annotations

import argparse
import json
import random
import re
import tempfile
import zlib
from collections import defaultdict
from pathlib import Path

import httpx

from hive_mcp.search.tokenize import STOPWORDS, tokenize

DATASET = "flax-sentence-embeddings/stackexchange_title_best_voted_answer_jsonl"
BASE = f"https://huggingface.co/datasets/{DATASET}/resolve/main"
TECH = ("serverfault.com", "softwareengineering.stackexchange.com", "dba.stackexchange.com",
        "unix.stackexchange.com", "security.stackexchange.com", "devops.stackexchange.com",
        "networkengineering.stackexchange.com")  # fmt: skip
LIFE = ("cooking.stackexchange.com", "gardening.stackexchange.com", "travel.stackexchange.com",
        "bicycles.stackexchange.com", "movies.stackexchange.com",
        "parenting.stackexchange.com")  # fmt: skip
FETCH_BYTES = 9_000_000
ANSWER_CHARS = (250, 1200)
CODEISH = re.compile(r"[A-Za-z]\d|\d[A-Za-z]|[A-Za-z][_/.\-][A-Za-z]|[a-z][A-Z]")
SENTENCE = re.compile(r"(?<=[.!?])\s+")


def fetch(site: str, cache: Path) -> list[tuple[str, str]]:
    path = cache / f"{site}.gz"
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        resp = httpx.get(
            f"{BASE}/{site}.jsonl.gz",
            headers={"Range": f"bytes=0-{FETCH_BYTES}"},
            follow_redirects=True,
            timeout=300,
        )
        resp.raise_for_status()
        path.write_bytes(resp.content)
    raw = zlib.decompressobj(31).decompress(path.read_bytes()).decode("utf-8", "ignore")
    pairs = []
    for line in raw.split("\n")[:-1]:  # the last line of a partial download is cut
        title, answer = json.loads(line)
        pairs.append((title.strip(), answer.strip()))
    return pairs


def first_sentence(answer: str) -> str:
    head = SENTENCE.split(answer.replace("\n", " "), maxsplit=1)[0]
    return head if len(head) <= 90 else head[:90].rsplit(" ", 1)[0]


def pick_heldout(answer: str, rng: random.Random) -> tuple[str, str] | None:
    sentences = SENTENCE.split(answer)
    options = []
    for i, s in enumerate(sentences[1:], start=1):
        letters = sum(c.isalpha() or c == " " for c in s)
        if 60 <= len(s) <= 220 and len(s.split()) >= 9 and letters / len(s) > 0.9:
            options.append(i)
    if not options:
        return None
    i = rng.choice(options)
    rest = " ".join(s for j, s in enumerate(sentences) if j != i)
    return (sentences[i], rest) if len(rest) >= 150 else None


def plant(rng: random.Random, used: set[str]) -> tuple[str, str]:
    """A unique id and a sentence that mentions it."""
    while True:
        kind = rng.choice(("ticket", "code", "host"))
        if kind == "ticket":
            ident = f"OPS-{rng.randrange(10000, 99999)}"
            line = f"Tracked in ticket {ident}."
        elif kind == "code":
            number = rng.randrange(1000, 9999)
            letters = "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(2))
            ident = f"HV-{number}-{letters}"
            line = f"The client reports error code {ident} when this fails."
        else:
            role = rng.choice(("db", "cache", "web", "queue", "build"))
            region = f"{rng.choice(('eu', 'us', 'ap'))}{rng.randrange(1, 9)}"
            ident = f"{role}-{region}-{rng.randrange(100, 999)}"
            line = f"First seen on host {ident}."
        if ident.lower() not in used:
            used.add(ident.lower())
            return ident, line


def build(sizes: list[int], core: int, seed: int, cache: Path) -> dict:
    rng = random.Random(seed)
    pool = []
    for site in TECH:
        pool += [
            (site, t, a)
            for t, a in fetch(site, cache)
            if ANSWER_CHARS[0] <= len(a) <= ANSWER_CHARS[1] and len(t) >= 25
        ]
    rng.shuffle(pool)
    seen, rows = set(), []
    for site, title, answer in pool:
        head = first_sentence(answer)
        if title not in seen and len(head) >= 35 and len(head.split()) >= 6:
            seen.add(title)
            rows.append((site, title, answer))
        if len(rows) == max(sizes):
            break
    used: set[str] = set()
    docs, planted = [], {}
    for n, (site, question, answer) in enumerate(rows):
        doc = {
            "id": f"d{n:05d}",
            "title": first_sentence(answer),
            "body": answer,
            "tags": [site.split(".")[0]],
            "q": question,
        }
        docs.append(doc)
    queries = []
    for n, doc in enumerate(docs[:core]):
        answer = doc["body"]
        queries.append({"q": doc.pop("q"), "relevant": [doc["id"]], "type": "question"})
        held = pick_heldout(answer, rng)
        if held:
            queries.append({"q": held[0], "relevant": [doc["id"]], "type": "heldout"})
            doc["body"] = held[1]
        if n % 2 == 0:
            ident, line = plant(rng, used)
            planted[doc["id"]] = ident
            doc["body"] += f"\n\n{line}"
    for doc in docs[core:]:
        doc.pop("q")
        if rng.random() < 0.3:  # distractors carry ids of the same shape
            doc["body"] += "\n\n" + plant(rng, used)[1]
    index: dict[str, set[str]] = defaultdict(set)
    for doc in docs:
        for tok in set(tokenize(f"{doc['title']}\n{doc['body']}")):
            index[tok].add(doc["id"])

    def holders(text: str) -> list[str]:
        toks = set(tokenize(text))
        return sorted(set.intersection(*(index[t] for t in toks))) if toks else []

    for n, doc in enumerate(docs[:core]):
        if doc["id"] in planted:
            ident = planted[doc["id"]]
            ctx = [w for w in tokenize(doc["title"]) if w not in STOPWORDS and len(w) > 3][:3]
            kind, text = (
                ("planted", ident) if n % 4 == 0 else ("planted-ctx", " ".join([ident, *ctx]))
            )
            queries.append({"q": text, "relevant": [doc["id"]], "type": kind})
            continue
        words = [w.strip(".,;:()\"'`[]{}<>!?") for w in doc["body"].split()]
        cands = [w for w in words if 5 <= len(w) <= 40 and CODEISH.search(w) and "http" not in w]
        rng.shuffle(cands)
        for w in cands:
            if holders(w) == [doc["id"]]:
                queries.append({"q": w, "relevant": [doc["id"]], "type": "identifier"})
                break
    for q in queries:
        if q["type"].startswith("planted") or q["type"] == "identifier":
            q["relevant"] = holders(q["q"])
    life = [t for site in LIFE for t, _ in fetch(site, cache)[:3000] if len(t) >= 25]
    queries += [{"q": t, "relevant": [], "type": "negative"} for t in rng.sample(life, 90)]
    return {
        "name": f"stackexchange-scale-{max(sizes)}",
        "sizes": sizes,
        "core": core,
        "docs": docs,
        "queries": queries,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--sizes", default="300,1000,5000,20000")
    ap.add_argument("--core", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--cache", type=Path, default=Path(tempfile.gettempdir()) / "eval_scale_src")
    args = ap.parse_args()
    data = build([int(s) for s in args.sizes.split(",")], args.core, args.seed, args.cache)
    args.out.write_text(json.dumps(data, ensure_ascii=False))
    kinds: dict[str, int] = defaultdict(int)
    for q in data["queries"]:
        kinds[q["type"]] += 1
    print(len(data["docs"]), "docs;", dict(kinds))


if __name__ == "__main__":
    main()
