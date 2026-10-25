# Embeddings

`HIVE_EMBEDDER` selects the embedder. Vectors are stored per `model_id` and never compared across
models, so switching is safe; items must then be re-indexed (a `hive reindex` command is planned).

| Value     | What                                   | Needs                                              |
|-----------|----------------------------------------|----------------------------------------------------|
| `hashing` | word-overlap only, no meaning          | nothing. Dev and tests only.                       |
| `onnx`    | offline bge-small-en-v1.5 (int8, 384d) | `pip install hive-mcp[local-embeddings]`, `hive models fetch` |
| `openai`  | any OpenAI-compatible `/embeddings`    | `HIVE_EMBEDDING_URL`, `HIVE_EMBEDDING_API_KEY`, `HIVE_EMBEDDING_MODEL`, `HIVE_EMBEDDING_DIM` |

`hive models fetch` downloads pinned files and verifies SHA-256; nothing is downloaded at server
start. Set `HIVE_EMBEDDING_AZURE=true` for Azure OpenAI (`api-key` header).

**Privacy:** with `openai`, item text is sent to the provider. Use `onnx` where that is not allowed.

## Relevance floor
Nearest neighbours always exist, so each embedder has a `min_similarity` below which a vector hit is
dropped. For bge-small it is 0.55: on a small hand-made set relevant pairs scored >= 0.57 and
unrelated ones <= 0.53 (hashing: 0.15-0.34 vs <= 0.34, top-1 5/6 vs 6/6 for ONNX). The set is small;
recalibrate on your own data. The floor for `openai` is `HIVE_EMBEDDING_MIN_SIMILARITY` (default 0.3,
model dependent).
