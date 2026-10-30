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
hybrid on this set, which is dominated by paraphrases. Sets with many exact names or error codes favour the
keyword list - and at realistic corpus sizes the fusion above under-serves exactly those queries, see "Scale" below.

## Scale: 300 to 20,000 notes

`evals/core.json` has 48 documents. Does the ranking still hold at the size of a real team's notes? Measured with the
real model (bge-small int8), the in-memory keyword (BM25) and exact vector indexes that `hive eval` uses, and the
production fusion code (`search.ranking.fuse`).

**Data.** We have no customer notes. The corpus is public text that resembles team knowledge: Stack Exchange
question-and-best-answer pairs from seven technical sites (serverfault, unix, dba, security, softwareengineering, devops,
networkengineering; Hugging Face dataset `flax-sentence-embeddings/stackexchange_title_best_voted_answer_jsonl`, CC BY-SA),
answers of 250-1200 characters. A note is the answer; its title is the answer's first sentence. Corpora are nested
(300, 1,000, 5,000, 20,000) and every query comes from the first 300 notes, so the *same* queries are asked at every size
and only the distractors grow. Reproduce with `scripts/eval_scale_data.py` (builds the set, seeded) and
`scripts/eval_scale.py` (scores it; `HIVE_EMBEDDER=onnx`, `HIVE_MODELS_DIR`).

**Queries and labels** are derived, not judged:
- *question*: the Stack Exchange question title (someone else's words) -> the answer it belongs to (300 queries).
- *held-out sentence*: one sentence cut out of the answer, which is then missing from the note (235).
- *identifier*: a code-like word of the answer (a path, flag or error name) that no other note contains (49).
- *planted id*: a ticket, error code or host name (`OPS-48213`, `HV-7731-QX`, `db-eu3-217`) planted in half of the 300
  notes and in 30% of the distractors (so ids are common, not a giveaway); the query is the bare id (75), or the id plus
  three words of the title (75). The relevant set of identifier and planted queries is every note containing all query tokens.
- *unrelated*: 90 question titles from cooking, gardening, travel, cycling, film and parenting sites.

R@1 / R@10 per query type, and the share of unrelated queries that still returned something:

| docs | mode | question | held-out sentence | identifier | planted id | planted id + words | unrelated answered |
|---|---|---|---|---|---|---|---|
| 300 | keyword | 0.49 / 0.75 | 0.57 / 0.78 | 1.00 / 1.00 | 0.99 / 1.00 | 1.00 / 1.00 | 0.90 |
| 300 | vector | 0.64 / 0.88 | 0.60 / 0.81 | 0.65 / 0.82 | 0.19 / 0.32 | 0.67 / 0.83 | 0.19 |
| 300 | hybrid (default) | 0.59 / 0.90 | 0.61 / 0.85 | 0.80 / 0.94 | 0.27 / 0.69 | 0.79 / 0.97 | 0.91 |
| 300 | hybrid + lead 1.5 | 0.59 / 0.90 | 0.61 / 0.85 | 0.84 / 0.96 | 0.89 / 0.99 | 0.91 / 1.00 | 0.91 |
| 300 | hybrid, vector weight 1 | 0.59 / 0.87 | 0.61 / 0.85 | 0.88 / 1.00 | 0.49 / 1.00 | 0.84 / 1.00 | 0.91 |
| 1000 | keyword | 0.42 / 0.64 | 0.46 / 0.69 | 0.98 / 1.00 | 0.99 / 1.00 | 1.00 / 1.00 | 0.98 |
| 1000 | vector | 0.57 / 0.79 | 0.49 / 0.72 | 0.35 / 0.73 | 0.19 / 0.27 | 0.64 / 0.77 | 0.36 |
| 1000 | hybrid (default) | 0.50 / 0.81 | 0.51 / 0.77 | 0.63 / 0.86 | 0.28 / 0.41 | 0.75 / 0.89 | 0.99 |
| 1000 | hybrid + lead 1.5 | 0.50 / 0.81 | 0.51 / 0.77 | 0.71 / 0.94 | 0.87 / 0.95 | 0.89 / 1.00 | 0.99 |
| 1000 | hybrid, vector weight 1 | 0.52 / 0.79 | 0.52 / 0.78 | 0.88 / 1.00 | 0.65 / 1.00 | 0.87 / 1.00 | 0.99 |
| 5000 | keyword | 0.31 / 0.52 | 0.32 / 0.58 | 0.94 / 1.00 | 0.99 / 1.00 | 1.00 / 1.00 | 0.99 |
| 5000 | vector | 0.40 / 0.64 | 0.31 / 0.60 | 0.22 / 0.57 | 0.16 / 0.21 | 0.56 / 0.71 | 0.48 |
| 5000 | hybrid (default) | 0.35 / 0.65 | 0.40 / 0.65 | 0.55 / 0.78 | 0.27 / 0.31 | 0.71 / 0.77 | 0.99 |
| 5000 | hybrid + lead 1.5 | 0.35 / 0.65 | 0.40 / 0.65 | 0.67 / 0.90 | 0.85 / 0.89 | 0.93 / 1.00 | 0.99 |
| 5000 | hybrid, vector weight 1 | 0.37 / 0.64 | 0.40 / 0.66 | 0.76 / 1.00 | 0.75 / 1.00 | 0.89 / 1.00 | 0.99 |
| 20000 | keyword | 0.24 / 0.42 | 0.24 / 0.46 | 0.90 / 1.00 | 0.99 / 1.00 | 1.00 / 1.00 | 1.00 |
| 20000 | vector | 0.28 / 0.53 | 0.23 / 0.46 | 0.12 / 0.37 | 0.12 / 0.19 | 0.51 / 0.64 | 0.64 |
| 20000 | hybrid (default) | 0.28 / 0.55 | 0.28 / 0.49 | 0.37 / 0.65 | 0.21 / 0.21 | 0.68 / 0.73 | 1.00 |
| 20000 | hybrid + lead 1.5 | 0.28 / 0.55 | 0.28 / 0.49 | 0.49 / 0.78 | 0.73 / 0.73 | 0.95 / 1.00 | 1.00 |
| 20000 | hybrid, vector weight 1 | 0.28 / 0.54 | 0.30 / 0.51 | 0.63 / 0.98 | 0.64 / 1.00 | 0.85 / 1.00 | 1.00 |

**What it shows.**
- *The observation from the stress test is a real quality problem, for one kind of query.* Exact tokens (ids, error
  codes, rare names) are found by keyword search at every size (R@1 0.90-1.00), yet the default hybrid ranks the planted id
  first for 27% of queries at 300 notes and 21% at 20,000, and has it in the top 10 for 69% and 21%. The hybrid is
  *worse than keyword alone* there by a wide margin.
- *Why.* Fusion gives the keyword list weight 1 and the vector list weight 2, so a note found only by keyword search scores
  at most `1/61`, less than a vector hit at any rank up to 62. Vector search cannot find an id (the embedding model has no
  idea what `OPS-48213` means; the right note was in its list for only 31-36% of these queries), but it always has
  neighbours: with the bge floor of 0.58 the vector list held on average 16 hits for a planted-id query at 300 notes, 32 at
  1,000 and 47 (of at most 50) at 5,000. Once the list is nearly full, a keyword-only note cannot reach the top 10. This
  is why it appears "at about 2,000 notes": it is where the list fills up, not a threshold in the code.
- *Paraphrase queries are not hurt by scale in a way fusion can fix.* Every mode loses recall as the corpus grows
  (question R@10: hybrid 0.90 at 300 notes, 0.55 at 20,000; keyword 0.75 and 0.42); hybrid is at or above both lists
  for R@10, and 0.05-0.07 below vector alone for R@1 on question queries up to 5,000 notes. Part of the drop is the label: a Stack Exchange topic has many
  near-duplicate answers and only one counts.
- *Unrelated queries almost always return something* in hybrid mode once the corpus has a few hundred notes (0.90-1.00,
  against 0.17 on the 48 short documents of `evals/core.json`), because the keyword list matches any shared word. Only the
  vector floor can say "nothing found", and with real text and a 20,000-note corpus 64% of unrelated questions still
  pass it. Treat "no result" as unreliable at this size.

**Settings that were tried** (macro-average over the five positive query types; R@1 / MRR):

| docs | default | lead 1.5 | lead 2.0 | vector weight 1 | floor 0.70 | floor 0.65 | keyword top 3 | keyword top 20 | vector weight 3 |
|---|---|---|---|---|---|---|---|---|---|
| 300 | 0.61 / 0.70 | 0.77 / 0.83 | 0.74 / 0.81 | 0.68 / 0.78 | 0.83 / 0.86 | 0.75 / 0.81 | 0.62 / 0.70 | 0.60 / 0.69 | 0.61 / 0.70 |
| 1000 | 0.54 / 0.62 | 0.70 / 0.77 | 0.65 / 0.73 | 0.69 / 0.77 | 0.76 / 0.81 | 0.64 / 0.73 | 0.56 / 0.63 | 0.52 / 0.61 | 0.53 / 0.61 |
| 5000 | 0.45 / 0.53 | 0.64 / 0.71 | 0.55 / 0.62 | 0.63 / 0.72 | 0.65 / 0.72 | 0.49 / 0.60 | 0.46 / 0.53 | 0.43 / 0.51 | 0.45 / 0.52 |
| 20000 | 0.36 / 0.43 | 0.55 / 0.61 | 0.47 / 0.53 | 0.54 / 0.64 | 0.51 / 0.60 | 0.36 / 0.46 | 0.37 / 0.43 | 0.34 / 0.41 | 0.35 / 0.41 |

The keyword depth (3 to 50) and the vector weight above 2 change nothing that matters; only a vector weight near 1, a
stricter floor, or an explicit lead for the best keyword hit fix the exact-token queries.

**What was changed, and what was not.**
- *Default ranking: unchanged.* No setting improved every query type at every size without costing something on the shipped
  sets. A vector weight of 1 is the best all-round candidate on this data but lowers `evals/core.json` hybrid R@1 from 0.85
  to 0.80 (MRR 0.91 to 0.88). A floor of 0.70 helps exact tokens but costs R@10 on paraphrases (0.90 to 0.75 at 300 notes)
  and the floor is embedder-specific.
- *New, off by default:* `HIVE_KEYWORD_LEAD_RATIO` (`RankingConfig.keyword_lead_ratio`, default 0). With a value such as 1.5,
  the best keyword hit is lifted to rank 1 if its score is at least that many times the second best (two or more hits) and
  vector search did not return it: the situation "one note matches the rare token far better than anything else, and the
  embedding model is blind to it". It leaves the paraphrase columns above identical at every size and lifts planted-id R@1
  from 0.21-0.28 to 0.73-0.89. Its cost on the shipped sets: `evals/core.json` hybrid R@1 0.85 to 0.84, and on
  `evals/multilingual.json` with e5 R@1 0.68 to 0.57 (three of 28 queries), which is why it is not the default. Larger
  ratios are safer and weaker (2.0 and 3.0 are in the script output). `hive eval` applies the setting, so you can test it on
  your own set before enabling it.
- The ratio compares keyword scores, which differ by backend. On the 2,000-note prefix of this corpus the best/second ratio
  of a planted-id query has median 2.3 (BM25 in memory), 2.3 (SQLite FTS5) and 1.5 (Postgres `ts_rank`, which has no
  idf); for paraphrase queries it is 1.1-1.2 on all three. So a ratio of 1.5 flags about nine in ten planted-id queries
  on every backend (borderline on Postgres, where 1.3 gives margin) and 10-20% of paraphrase queries; the lead then
  still acts only if the vector list lacks that note. Its cost on the shipped sets is given above.

**Limits of this evidence.** Stack Exchange answers are public, English, technical and written for strangers; team notes are
shorter, more local and full of names. Planted ids are artificial (their frequency and shape are our choice). The question
and held-out labels count one note as right where several may be; absolute recall is therefore too low for every mode, the
comparison between modes is the point. One embedding model, one chunker, 300 queries per type: differences under about
0.05 are noise. The in-memory keyword index is BM25; the Postgres index uses `ts_rank` and was only checked for score
ratios, not run end to end.

**On your own data.** Put exact-token queries in your evaluation set (ticket ids, error codes, host and product names as
people type them), run `hive eval` at your real corpus size, and compare with `HIVE_KEYWORD_LEAD_RATIO=1.5`. If many of
your searches are identifiers, enable it; if they are all questions in plain words, leave it off.

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
