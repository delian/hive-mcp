# Embeddings

`HIVE_EMBEDDER` selects the embedder. Vectors are stored per `model_id` and never compared across
models, so switching is safe; run `hive reindex --org <id>` afterwards: it queues one job per item and the server's job runner
re-embeds them (no restart; repeating is safe). Until done, consult falls back to keyword hits for
unindexed items. Vectors of the old model stay stored and unused.

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
