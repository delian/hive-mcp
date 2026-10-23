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

## Caveats
Synthetic data and a single machine. Treat results as relative. Re-run with real embeddings before the final choice.
