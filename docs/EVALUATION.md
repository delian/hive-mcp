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
