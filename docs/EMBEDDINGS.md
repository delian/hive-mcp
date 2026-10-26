# Embeddings

`HIVE_EMBEDDER` selects the embedder. Vectors are stored per `model_id` and never compared across
models, so switching is safe; run `hive reindex --org <id>` afterwards: it queues one job per item and the server's job runner
re-embeds them (no restart; repeating is safe). Until done, consult falls back to keyword hits for
unindexed items. Vectors of the old model stay stored (and unused) until you remove them:
`hive vectors status` shows each model's item count and how much of the data the current model covers;
`hive vectors prune --yes` deletes the other models' vectors, and refuses while the current model covers under
95% of items (`--force` overrides). Items without any text have no vectors, so coverage can stay below 100%.

| Value     | What                                   | Needs                                              |
|-----------|----------------------------------------|----------------------------------------------------|
| `hashing` | word-overlap only, no meaning          | nothing. Dev and tests only.                       |
| `onnx`    | offline bge-small-en-v1.5 (int8, 384d) | `pip install hive-mcp[local-embeddings]`, `hive models fetch` |
| `openai`  | any OpenAI-compatible `/embeddings`    | `HIVE_EMBEDDING_URL`, `HIVE_EMBEDDING_API_KEY`, `HIVE_EMBEDDING_MODEL`, `HIVE_EMBEDDING_DIM` |

`hive models fetch` downloads pinned files and verifies SHA-256; nothing is downloaded at server
start. Set `HIVE_EMBEDDING_AZURE=true` for Azure OpenAI (`api-key` header).

**Privacy:** with `openai`, item text is sent to the provider. By default only items shared to a project
or wider are sent (`HIVE_EMBEDDING_MIN_SCOPE=project`); private notes and session logs stay keyword-only,
and are embedded the moment they are promoted. Search *queries* are always sent to the provider. Use `onnx`
where nothing may leave the server.

## Relevance floor
Nearest neighbours always exist, so each embedder has a `min_similarity` below which a vector hit is
dropped. For bge-small it is 0.55: on a small hand-made set relevant pairs scored >= 0.57 and
unrelated ones <= 0.53 (hashing: 0.15-0.34 vs <= 0.34, top-1 5/6 vs 6/6 for ONNX). The set is small;
recalibrate on your own data. The floor for `openai` is `HIVE_EMBEDDING_MIN_SIMILARITY` (default 0.3,
model dependent).

## Choosing a local model (`HIVE_EMBEDDING_MODEL`)
Measured with `hive eval` (docs/EVALUATION.md); both are 384-dimension int8 models that run on CPU:

| model | languages | English set R@3 / MRR | multilingual set R@3 | floor |
|---|---|---|---|---|
| `bge-small-en-v1.5-int8` (default) | English | 0.97 / 0.92 | 0.61 | 0.58 |
| `multilingual-e5-small-int8` | about 100 | 0.94 / 0.89 | 0.82 | 0.81 |

Use the multilingual model when notes are written in several languages (queries then also work across languages:
an English question finds a German note). Switching models means `hive models fetch <name>`, `hive reindex`, wait for
`hive vectors status` to reach 100%, then `hive vectors prune --yes`. The two models need different relevance floors
because e5 cosines are compressed upward; the floors above are built in.

## Reranker (`HIVE_RERANKER=onnx`, off by default)
A cross-encoder (`ms-marco-minilm-l6-int8`, 23 MB, English) re-reads the best `HIVE_RERANK_DEPTH` (15) candidates
together with the query and reorders them. On `evals/core.json` it lifts MRR from 0.91 to 0.93 and recall@1 from
0.85 to 0.89, while recall@5 dips by 2 points; it did not help paraphrases it could not see (about 12% of relevant
pairs score as unrelated), so it **only reorders and never drops results**. It is English-only, so leave it off for
multilingual notes. Cost: a few tens of milliseconds per search on CPU. Fetch with `hive models fetch ms-marco-minilm-l6-int8`.
