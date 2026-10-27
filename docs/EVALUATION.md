# Retrieval evaluation

`hive eval evals/core.json [--sweep]` indexes a labelled set in memory with the configured embedder
(`HIVE_EMBEDDER`) and reports recall@k, MRR, how often a query returned nothing, and the false-positive rate on
"negative" queries that should find nothing. Use your own set with the same JSON shape
(`docs: [{id,title,body}]`, `queries: [{q, relevant: [ids], type}]`) - **do this on a sample of your real notes**;
numbers on someone else's text only say how the system behaves in general.

`evals/core.json` is small (48 short documents in 9 areas, 96 positive queries: half paraphrases that share
almost no words with the document, half keyword-style; 12 unrelated negatives). It was written by the
developers, and the ranking settings below were tuned on it, so treat the figures as optimistic.

| embedder | mode | R@1 | R@3 | R@10 | MRR | negatives answered |
|---|---|---|---|---|---|---|
| hashing-256 | keyword | 0.69 | 0.76 | 0.79 | 0.73 | 0.17 |
| hashing-256 | hybrid | 0.66 | 0.72 | 0.79 | 0.70 | 0.58 |
| bge-small (onnx) | keyword | 0.69 | 0.76 | 0.79 | 0.73 | 0.17 |
| bge-small (onnx) | vector, floor 0.58 | 0.88 | 0.97 | 0.97 | 0.92 | 0.00 |
| bge-small (onnx) | hybrid (default) | 0.85 | 0.97 | 0.99 | 0.91 | 0.17 |

What the evaluation changed:
- **Stopwords:** queries used to match any document containing "the" or "for" (83% of unrelated queries returned
  results). Query terms now drop common English words (documents keep them): 17%.
- **Fusion:** plain rank fusion of keyword and vector results made a good embedding model *worse* (MRR 0.86 vs 0.93
  for vectors alone), because mediocre keyword hits were treated as equals. Fusion now weights the vector list x2 and
  uses only the top 5 keyword hits (MRR 0.91). Exact identifiers and names still surface through the keyword list.
- **Relevance floor:** bge-small's floor is 0.58 (was 0.55): no unrelated query passes it, recall@3 stays 0.97.
Keyword retrieval is the weak part on paraphrase-heavy queries; vectors alone remain slightly better than the
hybrid on this set, which is dominated by paraphrases. Sets with many exact names or error codes will favour the
keyword list - which is why it is kept.

## Multilingual set
`evals/multilingual.json`: 14 short documents (German, French, Spanish, Bulgarian, Italian), each with a
same-language paraphrase query and an English query for the same document, plus 6 unrelated queries.
bge-small (English-only) reaches recall@3 0.61; multilingual-e5-small 0.82 (same-language and cross-language
queries together). Floors: the e5 floor of 0.81 is a compromise between the English set (R@3 0.92 there; 0.94 at
no floor) and this one; e5 separates relevant from unrelated less sharply than bge does.

## Evaluating on your own data

The shipped sets are small and were used to tune the defaults, so measure on your notes before trusting a floor.

**1. Collect real labels** (opt-in): set `HIVE_SEARCH_FEEDBACK=true`. `kb_consult` then records the search text (secrets
masked, 200 characters at most) with the ids it returned, and `kb_get` records which item was opened, in the audit trail
(`search.query`, `search.open`; admins can read it with `kb_admin_audit`). Search texts show what people work on, so tell
them, and leave it off if that is not acceptable.

**2. Build a set** after a few weeks:
```bash
hive eval-build real.json --org acme --days 30 --distractors 50 --negatives evals/core.json
```
A search followed within 30 minutes by opening one of its results counts as "that result is relevant to that search".
`--negatives` borrows the unrelated queries from another set, because real use has no "nothing should be found" labels.
The file contains real item text (private items included): it is written with mode 0600; keep it out of git and delete it
when done. Labels are weak (people open results that were not the answer): skim the file, delete wrong pairs, and expect
anecdotes until there are a few dozen queries.

**3. Score it and pick a floor**:
```bash
hive eval real.json --recommend
```
sweeps the vector relevance floor in steps of 0.01 and prints `Suggested: HIVE_RELEVANCE_FLOOR=0.xx`: the floor with the best
recall@3 among those that answer at most 5% of the unrelated queries. Set `HIVE_RELEVANCE_FLOOR` to apply it to the
configured embedder (any kind). Re-run when you change embedder or reranker; compare `hive eval` output before and after
a change to catch regressions.

## Telling the server what helped (`kb_rate`) and finding gaps

With `HIVE_SEARCH_FEEDBACK=true`, agents are told (in the server instructions) to call `kb_rate(id, useful)` after
`kb_consult` once they know whether a result helped. A rating counts more than an open, and it is the only way to say
"no": `hive eval-build` credits a "useful" to the search that showed the item (or the `query` the agent passed) and drops
any result rated "not useful" for that search, even if it had been opened. Ratings are stored in the audit trail
(`search.rating`, admins only).

`hive feedback-report --org acme --days 30` lists what to fix: searches that **found nothing** (notes to write),
searches that **found something nobody used** (notes that are missing or badly titled), the most useful notes and the
ones rated not useful (candidates for `kb_review_stale`). It reads the same trail; nothing is recorded when the
setting is off, and `kb_rate` then reports `recorded: false`.
