# Storage benchmark (Phase 0)

Harness: `bench/` (run `uv run python -m bench.run <pg|lance|duck|sqlitevec> <n> <queries>`).
Machine: 4 vCPU, 15 GB RAM, local disk. Postgres 16 + pgvector 0.8.1 (built from source), LanceDB 0.40,
DuckDB 2.0, sqlite-vec 0.1.9. Postgres ran with `fsync=off` and 4 GB shared buffers (bulk-load benchmark settings).

## Method
- Synthetic, clustered, unit-norm 384-dim vectors (float16-rounded; identical for every engine), pseudo-text with Zipf
  vocabulary, 2000 groups. Query vectors are noisy copies of stored chunks.
- Exact ground truth computed with numpy. Recall@10 against it.
- "ACL" queries are restricted to the 5 groups a principal can see (about 0.65-1% of chunks): the hard filtered-ANN case.
- Keyword queries: 2 topic words + the 2 rarest words of a source chunk (AND semantics); "hit" = source chunk in top 10.
- Single-threaded sequential queries after one warm-up; no concurrency, no writes during queries.

## 1M chunks, tuned (about 95%+ recall target)
| engine / settings | vec recall | vec p50 / p99 ms | ACL recall | ACL p50 / p99 ms | build s | disk MB |
|---|---|---|---|---|---|---|
| pgvector HNSW m=32 efc=200, ef_search=200 | 0.983 | 4.1 / 17 | 0.975 | 7.2 / 408 | 499 | 2.6k |
| pgvector HNSW m=16 efc=64 (default-ish), ef_search=800 | 0.909 | 9.0 / 24 | 0.907 | 11.3 / 264 | 149 | 2.4k |
| LanceDB IVF_FLAT, 1000 partitions, nprobes=150 | 1.000 | 24.5 / 367 | 0.977 | 8.6 / 17 | 32 | 3.2k |
| DuckDB exact scan | 1.000 | 184 / 202 | 1.000 | 67 / 82 | 0 | 1.8k |
| sqlite-vec brute force | 0.996 | 611 / 668 | 0.998 | 314 / 350 | 0 | 1.7k |

Keyword search at 1M (hit@10 = 1.00 everywhere): Postgres GIN 0.2 ms p50, sqlite FTS5 0.3 ms, LanceDB 4.4 ms, DuckDB 126 ms.
At 100k, same ordering; pgvector (m=16) recall 0.975 and LanceDB IVF_FLAT 1.000.

## Findings
1. pgvector needs a denser graph at 1M (m=32, ef_construction=200): recall 0.74 -> 0.98 at similar latency, but the
   build took 8.3 min on 3 workers. Extrapolate to tens of millions of vectors with care (hours, RAM-bound).
2. pgvector's filtered-query p99 stays around 260-520 ms for every ef_search: a few very selective filters make the
   iterative scan walk far. Median is excellent. Needs mitigation (partial indexes / partitioning by group, or a
   brute-force path when the allowed set is small) before relying on it.
3. LanceDB is stronger on filtered search tail latency (p99 17 ms) but has a slow unfiltered tail (p99 367 ms,
   undiagnosed) and its default compressed index (IVF_PQ) only reached about 0.90 recall on this data.
4. DuckDB and sqlite-vec are exact but linear; usable as a small-deployment mode up to roughly a few hundred thousand chunks.
5. Not measured: concurrency, writes/updates under load, memory use of each engine, 10M+ scale, real embeddings
   (synthetic clusters may favour or hurt ANN indexes differently), hybrid fusion quality, Azure Files behaviour.

## Real data (220k chunks, 384-dim)
Corpus: Simple English Wikipedia (first 220,000 chunks of ~107 words, up to 5 per article), embedded with
bge-small-en-v1.5 (int8 ONNX, CLS pooling, normalised). Groups: hash of each article's first category (topic-correlated,
2000 buckets), 20% reassigned at random. Queries: a 12-word phrase from a stored chunk, embedded with the same model;
keyword queries: the 4 rarest corpus words of the source chunk. The visible set per ACL query averages 0.3% of chunks (about 660).
Postgres: HNSW m=32, ef_construction=200, ef_search=200, iterative scan on. LanceDB: IVF_FLAT, 469 partitions, nprobes=150.

| engine | vec recall | vec p50 / p99 ms | ACL recall | ACL p50 / p99 ms | keyword p50 / p99 ms | vec idx s | disk MB |
|---|---|---|---|---|---|---|---|
| pgvector HNSW | 0.990 | 6.2 / 11.5 | 1.000 | 1.1 / 20.9 | 0.2 / 0.3 | 90 | 729 |
| LanceDB IVF_FLAT | 0.994 | 13.3 / 31.8 | 0.961 | 6.4 / 7.7 | 4.6 / 6.1 | 8 | 802 |
| DuckDB exact | 1.000 | 81 / 96 | 1.000 | 31 / 46 | 51 / 65 | 0 | 471 |
| sqlite-vec brute force | 0.998 | 133 / 154 | 1.000 | 74 / 83 | 0.2 / 0.3 | 0 | 405 |

Keyword hit@10 = 1.00 for every engine.

### What the real data changes
- Rankings match the synthetic run; absolute recall is higher on real embeddings (pgvector 0.99 at m=32 here).
- pgvector's slow filtered tail did not appear: `EXPLAIN` shows the planner uses the `group_id` btree plus an exact sort when the
  visible set is small (about 1,000 of 220k rows: exact, recall 1.0) and HNSW when it is broad (about 10%).
  Hypothesis, not yet verified at 1M: the 400 ms tail on the synthetic 1M run came from the in-between zone (0.5-2% visible), where
  the planner picks HNSW and the iterative scan walks far. Test with real data at larger scale and with `EXPLAIN` before relying on it.
- LanceDB's filtered recall (0.96) is the lowest of the ANN engines: with a very selective filter some allowed rows sit in
  partitions that are not probed. Higher `nprobes` fixes it at some cost.
- Scale limit: embedding on this 4-vCPU machine runs at about 60 chunks/s, so the real corpus stops at 220k; 1M+ real-embedding
  results need a faster embedding source (GPU or hosted API).

## Caveats
Single machine, sequential queries, no concurrency or writes. The 1M results are synthetic; the real-embedding results stop at 220k.
