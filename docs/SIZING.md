# Sizing: how big can one Hive get?

Measured with `scripts/loadtest.py` on one 4-vCPU machine, 5,000 to 50,000 notes, SQLite and PostgreSQL.
Read the [caveats](#caveats-read-these-first) before quoting a number.

## The short version

| Question | Answer |
|---|---|
| How many notes on the default embedded profile (SQLite)? | **Comfortable to ~10k notes; fine to 50k only while few agents search at once.** Without the in-memory vector matrix a search reads every vector the caller may see, so its cost is linear: about **1.4 ms per 1,000 notes** (14 ms at 5k, 34 ms at 20k, 75 ms at 50k, one client alone); the matrix (default) makes the scan itself 50 to 70 times cheaper (table below) and the saturated 20k-note run 2.6 times faster. |
| When to move to PostgreSQL? | Above **~20k notes with more than ~10 agents active at once**, above **~50k notes** at all, when you want more than one replica, or when you want point-in-time recovery. At 50k notes, 24 agents hammering the server get **~88 calls/s on PostgreSQL against ~27 on SQLite**. |
| Memory? | Server process: **~140 MB at 5k, ~220 MB at 20k, ~350 MB at 50k** on SQLite (the page cache grows with the data); **~115 MB flat** with PostgreSQL (the database has its own memory). Add **~300 MB** for the real ONNX embedding model. No growth over time was found. |
| Disk? | **~6 to 7 KB per note** including indexes (hashing embedder, 256 dimensions; the 384-dimension bge model adds roughly 1 to 2 KB). Backups: an archive is about a quarter of the SQLite file. |
| CPU? | One server process is one event loop plus one database thread: it uses **1 to 1.5 cores** at saturation. 2 vCPU is enough for the app; the real embedder is the heavy part (~20 notes/s written on 4 vCPU). |
| Backup cadence? | The default (15 min) is fine to ~20k notes. At 50k a SQLite backup takes 30 to 47 s and visibly slows the server while it runs: back up hourly or off-hours there. |
| Restart / restore time? | Start-up is **1 to 2 s at every size**. Restoring SQLite takes 0.3 s (5k) to 2.5 s (50k). Restoring PostgreSQL takes **80 s at 50k** (the vector index is rebuilt). |

## How it was measured

- **Machine**: 4 vCPU, 15.7 GiB RAM, no swap, local virtual disk, Linux 6.18, Python 3.12. PostgreSQL 16 with pgvector ran on the
  same machine (`shared_buffers=512MB`; the main series used `fsync=off`, `synchronous_commit=off`; 5k and 20k were repeated
  with a durable instance, `fsync=on`: ingest 142 and 117 notes/s against 160 and 128, so it matters little on this disk).
  **The load generator and the server shared the 4 cores**, and other jobs ran on the machine (load average 2 to 7).
- **Data** (deterministic, `--seed`): three organisations (60 / 30 / 10 % of notes), one user per 250 notes (20 at 5k, 200 at
  50k), projects and teams per organisation, **40 % private, 35 % project, 10 % team, 15 % organisation-wide notes, 20 % of the
  shared ones visible to the whole organisation**. Bodies are Zipf-distributed synthetic words in 60 topics, median 90 words
  (~700 characters, about 1.5 chunks per note), 0 to 4 tags. Notes are written through the product's own write path
  (`KnowledgeService.remember`, 8 concurrent writers, indexed inline).
- **Embedder**: the built-in hashing embedder (256 dimensions) for the size series, so no model download is needed. It costs
  almost nothing; the real model is measured separately below.
- **Workload**: `hive serve` as a child process, driven over real HTTP on `/mcp` (rate limit off). 24 simulated agents, each
  one user with its own connection. Mix: `kb_consult` 62 %, `kb_get` 12 %, `kb_remember` 8 %, `kb_update` 5 %, `kb_list` 4 %,
  `kb_rate` 3 %, promotion requests and decisions 3 %, `kb_history` 3 %. Three phases per size:
  **saturated** (no think time, 24 closed-loop clients: a stress test, it shows the capacity), **paced** (10 calls/s in total,
  exponential gaps: about what 24 chatty agents produce), **idle** (one client alone: the cost of one request).
- Each phase is 45 to 90 s after a 5 s warm-up. Each size was measured on the same, growing database (5k, then 20k, then 50k).

## Results

### Writing: ingest rate while the corpus grows

| Notes | SQLite notes/s (remember p50 / p95) | PostgreSQL notes/s (p50 / p95) |
|---|---|---|
| to 5k | 296 to 327 (22 / 35 ms) | 129 to 160 (49 to 60 / 69 to 86 ms) |
| 5k to 20k | 281 to 296 (24 / 38 to 46 ms) | 107 to 128 (60 to 73 / 84 to 100 ms) |
| 20k to 50k | 274 to 285 (24 / 41 to 49 ms) | 99 to 123 (62 to 78 / 88 to 108 ms) |

SQLite writes do not slow down as the corpus grows. PostgreSQL slows by about a quarter (vector index inserts) but stays
above 100 notes/s. With `HIVE_EMBEDDER=onnx` (bge-small int8) the writer is CPU-bound: **~20 notes/s** on 4 vCPU, so an
initial import of 100k notes takes about 1.5 hours; plan for it.

### Reading and editing: latency against corpus size

p50 / p95 in milliseconds, `kb_consult` (search) and `kb_get` (read one note).

| Notes | SQLite, one client alone | SQLite, 10 calls/s | PostgreSQL, 10 calls/s | SQLite, saturated (24) | PostgreSQL, saturated (24) |
|---|---|---|---|---|---|
| 5k search | 14 / 18 | 15 / 28 | 17 / 28 | 237 / 309 | 175 / 289 |
| 20k search | 34 / 43 | 36 / 82 | 42 / 66 | 513 / 691 | 206 / 297 |
| 50k search | 75 / 94 | 84 / 251 | 61 / 96 | 1,057 / 1,434 | 295 / 393 |
| 5k to 50k get | 4 / 5 | 5 to 6 / 11 to 72 | 6 to 8 / 12 to 14 | 41 to 195 / 88 to 393 | 41 to 55 / 82 to 113 |
| 5k to 50k remember | 6 / 10 | 8 to 12 / 16 to 143 | 18 to 22 / 34 to 38 | 233 to 983 / 309 to 1,427 | 197 to 264 / 284 to 369 |
| Calls per second, saturated | | | | **117 / 55 / 27** | **141 / 128 / 88** |

Notes on the table:

- The paced PostgreSQL column was measured with an online `pg_dump` running during the phase, and the paced SQLite column
  with an online backup too in the earlier series; the SQLite paced p95 at 50k (251 ms) also reflects a noisy machine.
  The idle column is the cleanest measure of what one search costs.
- "Saturated" is 24 clients that never pause. Latency there is almost entirely queueing; read the calls per second.
- Writes and reads of one note stay at a few milliseconds when the server is not saturated, at every size.

### Where the first cliff is

> **The tables below were measured before the in-memory vector matrix** (`HIVE_VECTOR_CACHE_MB=0` reproduces them). With the
> matrix on, which is the default, the same saturated 20k-note run with 24 agents went from **59 to 154 calls/s** (two runs
> each: 60 and 58 against 156 and 153), median search from 489 and 506 ms to 176 and 181 ms, `kb_get` from 85 and 97 to 47 ms,
> `kb_remember` from 382 and 390 to 150 ms, server memory from 263 to 185 MB, still no errors. That is 2.6 times the
> throughput, not the 50 to 70 times of the scan alone: once the scan is cheap, HTTP, JSON and the writes are the cost.
> The cliff has moved, not vanished; re-measure with `scripts/loadtest.py` before relying on a size.

**SQLite, at about 20k notes with many concurrent agents.** Search is an exact scan over the caller's access-filtered
vectors, which is CPU work in the server process (numpy plus Python row handling), so searches compete with every other
request for the same cores.
Throughput falls with size (117, 55 and 27 calls/s at 5k, 20k and 50k) and a saturated `kb_get` goes from 41 to 195 ms.
Nothing breaks: there were **no errors in any run**; it just queues. Up to 20k notes a 24-agent fleet at a normal pace
(10 calls/s) still gets a median search under 40 ms. Past 50k notes one search alone costs 75 ms and about 10 concurrent
searches already saturate a core.

**Read connections do not move the cliff.** The embedded profile has one writer connection plus
`HIVE_SQLITE_READERS` read-only connections (default 1: a search no longer makes writes wait). Measured at 20k notes, 24
saturating agents, 4 vCPU (the load generator shares the cores), 45 s each, two runs for 0 and 4:

| Readers | Calls/s | Search p50 / p95 (ms) | `kb_get` p50 / p95 (ms) | Promotion p50 (ms) |
|---|---|---|---|---|
| 0 (one shared connection) | 59 | 480 / 650 | 90 / 180 | 397 |
| 1 (default) | 59 | 479 / 763 | 85 / 201 | 293 |
| 2 | 52 | 552 / 850 | 99 / 293 | 360 |
| 4 | 29 | 965 / 1,600 | 140 to 155 / 550 to 580 | 620 to 690 |

More readers make it worse here: parallel scans fight over the interpreter lock and the caches, so one reader is
the setting. The cure for a saturated SQLite server is PostgreSQL, or fewer notes per server, not more connections.

**PostgreSQL has no cliff up to 50k notes** in these runs, but search still grows with size (17, 42, 61 ms at 10 calls/s,
pgvector with an HNSW index plus the access filter). For 100k to 1M notes see [BENCHMARK](BENCHMARK.md) (ANN engines at scale).

### The in-memory vector matrix (built; how it scales)

Without the cache a SQLite search streams every visible vector out of the database and multiplies in Python batches.
With it (`HIVE_VECTOR_CACHE_MB`, default 512) one resident float32 matrix per model (one row per chunk) answers with a
single matrix-vector product and an access mask by row, and follows every writer through a trigger-fed change log. `bench/vecmatrix.py` measured that on synthetic 384-dimension unit vectors (4 vCPU, nothing else running,
exact results, so search quality is unchanged). Large texts matter because every chunk (about 100 to 120 words) is a
row: **one megabyte of text is roughly 1,500 chunks, about 2.4 MB of float32 vectors**, more than the text itself.

| Chunks (text) | Matrix RAM (float32) | Full scan p50 | Visible set 3% | Visible set 0.3% | Searches per second, 1 / 4 at once |
|---|---|---|---|---|---|
| 100k (~65 MB) | 0.15 GB | 2 ms | 0.9 ms | 0.0 ms | 441 / 332 |
| 500k (~330 MB) | 0.77 GB | 16 ms | 3.3 ms | 0.4 ms | 61 / 42 |
| 1M (~650 MB) | 1.5 GB | 27 ms | 7.9 ms | 0.7 ms | 37 / 22 |
| 2M (~1.3 GB) | 3.1 GB | 76 ms | 20 ms | 1.6 ms | 12 / 10 |

- **Against today:** the current scan costs about 1.4 ms per 1,000 chunks, so 100k chunks take ~140 ms and 1M take
  ~1.4 s; the matrix is 50 to 70 times faster at the same size.
- **It scales to about 1M chunks (650 MB of text) comfortably and to 2M (1.3 GB of text) with a 76 ms worst case.**
  Cost is linear and bound by memory bandwidth, not by Python: more threads do not add throughput (the product is
  already multi-threaded and memory-bound), so a saturated server tops out near 40 searches per second at 1M chunks.
- **Selecting rows by index (a gather) is only worth it for small visible sets.** At 30% visible the gather took 122 ms
  at 1M chunks against 27 ms for scanning everything and masking: use the gather below about 5% visible and the full
  scan above it.
- **What it costs:** RAM of 1.5 KB per chunk (float32; float16 halves it but numpy has no fast float16 product, so it
  would need a conversion per block); a rebuild while serving briefly holds two copies; a load of about one current search
  per start (about 2 s at 1M chunks).
- **Staying correct (built and tested):** triggers on the vector table log every change by any connection, process or cascade;
  a search reads the newest log number inside its read transaction and patches in only the items logged since (reloading when
  more than 20% of the items changed or the trimmed log no longer reaches back); dead rows are squeezed out when they pass 25%;
  the access mask is the same rule as the SQL filter, and `tests/adapters/test_sqlite_vector_cache.py` checks the answers
  against the scan for random principals, limits and data. A model that does not fit the cap is searched in the database.
- **What it does not change:** the first search after a start loads the model (about one old-style scan, 2 s at 1M chunks);
  memory is 1.5 KB per chunk (the default cap of 512 MB holds about 330,000 chunks of 384 dimensions, or 100 MB of text; raise it
  to hold more); and searches of one model take turns, which cost nothing measurable because the product is already
  multi-threaded and memory-bound.
- **Beyond about 2M chunks** (a 1.3 GB text corpus) use an approximate index: PostgreSQL with pgvector (about 4 to 6 ms
  at 1M chunks, [BENCHMARK](BENCHMARK.md)) or the LanceDB store.

### pg_duckdb against pgvector (measured, not adopted)

`pg_duckdb` 1.1.1 (DuckDB's engine inside PostgreSQL 16, built from source) next to pgvector 0.8.1 on the same server,
the same 100,000 synthetic 384-dimension chunks, queries and exact ground truth as the storage benchmark
([BENCHMARK](BENCHMARK.md)); `bench/pgduckdb_cmp.py`, 4 vCPU, nothing else running, one run each.

| Candidate | Unfiltered p50 / p95 (ms) | Access-filtered (0.9% visible) p50 / p95 (ms) | Recall@10 | Searches/s, 4 clients (unfiltered / filtered) |
|---|---|---|---|---|
| **pgvector HNSW** (halfvec, m=32, ef 200) | **4.2 / 6.4** | **1.0 / 5.0** | 0.999 | **692 / 1,346** |
| pgvector exact (no index) | 19.0 / 22.5 | 9.6 / 12.7 | 1.000 | 86 / 361 |
| pg_duckdb on a Postgres `real[]` table | 288 / 324 | 33 / 43 | 1.000 | 7.6 / 46 |
| pg_duckdb on an uncompressed Parquet file | 361 / 388 | 238 / 250 | 1.000 | 5.5 / 8.5 |

- **For similarity search pgvector wins by a wide margin:** about 70 times faster than pg_duckdb unfiltered and 30 times
  faster filtered, and even pgvector's exact scan is 15 times faster than pg_duckdb's. DuckDB's list and array inner
  products ran at about 0.35 million vectors per second against 50 million for one numpy matrix product (previous
  section); the fixed-size array function was only 20% faster than the list one (285 against 355 ms).
- **They do not combine:** pg_duckdb cannot read a pgvector `vector` or `halfvec` column ("Unsupported PostgreSQL type"
  in its planner); Postgres then runs the query itself with a warning, so nothing breaks, but nothing is accelerated.
  Its vectors have to be `real[]` (1.8 times the size of halfvec: 164 MB against 91 MB for 100k chunks) and its functions are
  reached through `duckdb.query(...)`.
- **Analytics is where it can help, and only sometimes** (5 million synthetic audit-like rows, same server): a light
  aggregation ran slower under pg_duckdb (about 430 ms, against 200 to 270 ms for plain PostgreSQL, whose scan is parallel),
  a heavy high-cardinality one ran 3.8 times faster (0.50 s against 1.9 s). Our reports (usage, audit counts, digest)
  work on tens of thousands of rows, so neither case would be noticed.
- **Cost of adopting it:** the extension must be preloaded and built or installed for the exact PostgreSQL major version
  (our image uses Debian's), managed Postgres offers it only as a preview behind an allow-list, and DuckDB brings its
  own memory and threads. Not adopted; `hive export` (Parquet) and DuckDB's own `postgres` extension against a read-only
  role cover analysis outside the server.
- **Limits of this test:** one size (100k chunks), synthetic clustered vectors, one run each, DuckDB's thread and memory
  settings left at their defaults, and `pg_duckdb` used as released, with no vendor tuning.

### Memory, threads, file descriptors

| Notes | SQLite server RSS (after the saturated phase) | PostgreSQL profile: server RSS | PostgreSQL's own memory (PSS) |
|---|---|---|---|
| 5k | 137 MB | 117 MB | 175 MB |
| 20k | 219 MB | 117 MB | 280 to 320 MB |
| 50k | 351 MB (peak 366) | 99 to 118 MB | ~610 MB |

PostgreSQL's figure includes the shared buffers it has touched (`shared_buffers=512MB` here); with the durable instance it
reached 700 to 820 MB at 5k to 20k. With the real ONNX model the server was 370 to 430 MB at 3k notes. **No leak**: during every
90 s paced phase RSS was flat (for example 138.3 to 138.6 MB at 5k, 209.5 to 209.7 MB at 20k, one MB or less of drift), and
threads (12 for SQLite, 4 for PostgreSQL) and open file descriptors (about 35: the 24 clients plus the server's own) did not
move. The growth with size on SQLite is the page cache and the numpy buffers of the vector scan. Graceful shutdown took 0.2 s.
`tests/stress/test_soak.py` repeats the flatness checks in 17 s.

### Disk

| Notes | SQLite file | PostgreSQL database | Backup archive: SQLite / PostgreSQL |
|---|---|---|---|
| 5k | 30 MB | 44 MB | 9.4 / 7.0 MB |
| 20k | 123 to 126 MB | 137 to 145 MB | 32 / 25 MB |
| 50k | 297 to 307 MB | 323 to 340 MB | 77 / 59 MB |

That is 6 to 7 KB per note, of which the vectors are about a quarter. Keep `HIVE_BACKUP_KEEP` archives (default 8): about
twice the database size in total on SQLite.

### Backup, restore, start-up

Seconds, from the `hive backup` / `hive restore` code paths (`create_backup`, `verify_backup`, `restore_backup`).

| Notes | SQLite backup + verify (idle) | SQLite backup while serving 10 calls/s | SQLite restore | PostgreSQL `pg_dump` + verify (idle) | PostgreSQL dump while serving | PostgreSQL `pg_restore` | Server start (ready) |
|---|---|---|---|---|---|---|---|
| 5k | 3.5 + 0.4 | 3.0 | 0.3 | 1.6 + 0.02 | 1.4 | 8.0 | 0.9 to 1.3 |
| 20k | 13.6 + 1.7 | 15.3 | 1.1 | 5.1 + 0.2 | 5.0 | 13.8 | 1.0 to 1.3 |
| 50k | 29.6 + 3.6 | 46.7 | 2.5 | 12.0 + 0.4 | 12.5 | **80.5** | 1.0 to 1.2 |

- A SQLite backup copies the whole file (online backup API), hashes it and compresses it, in a worker thread. At 50k, while
  it runs, search p95 reached about 1 s in a paced run (measured before the scan fix below; 94 ms without a backup
  after it): schedule big backups off-hours.
- `pg_restore` is slow because it rebuilds the HNSW and text indexes; that is your PostgreSQL recovery time objective at
  size, in addition to the time to start a database. Use the platform's point-in-time recovery for faster recovery.
- Start-up does not load indexes, so it is 1 to 2 s (2 s with the ONNX model) regardless of size.

### Background jobs

Searchable text and vectors are written inline with the note, so **no indexing backlog builds up**; the job queue held
only promotion work (apply and notification jobs). During the mixed runs it peaked at 3 to 30 open jobs with the oldest due
job 0.5 to 3 s old. The exception is saturation: with 24 never-pausing clients on SQLite at 5k notes up to **151 jobs were
queued and the oldest waited 19 s**, before the single-reader change and the faster vector scan (see below). It drained within
seconds once the clients paused.

**The background thread** (`HIVE_BACKGROUND_THREAD=true`) runs the job runner and the other loops on an event loop and
storage connections of their own. Measured at 5k notes, 24 saturating agents, one reader, two runs each (4 vCPU, other work
nearly idle but not controlled):

| Background thread | Calls/s | Search p50 | Peak open jobs | Oldest due job | Server RSS / threads |
|---|---|---|---|---|---|
| off (default) | 121 and 112 | 230 and 243 ms | 37 and 30 | 2.7 and 1.7 s | 177 to 184 MB / 12 |
| on | 115 and 107 | 237 and 255 ms | 18 and 18 | 0.95 and 0.56 s | 184 to 190 MB / 21 |

It roughly halves the backlog and its age, for about 5% fewer calls per second and a second set of storage connections
(~8 MB). It does not give the jobs a core of their own: it is one process, so the interpreter lock is shared. It stays
off by default; turn it on when heavy background work (connectors, bulk folder imports, group sync) should not queue behind
requests. If the thread cannot start, the server logs it and runs the jobs on the request loop. `/metrics` (needs `HIVE_METRICS_TOKEN`) computes vector coverage when
scraped: 26 to 35 ms idle, up to 0.2 to 1.0 s under saturation at 20k to 50k; scrape no more often than every 15 s.

### The real embedding model and LanceDB

| Setup | Result |
|---|---|
| `HIVE_EMBEDDER=onnx`, 3k notes, SQLite | write 20 notes/s (p50 320 ms); 10 calls/s: search 34 / 69 ms, remember 57 / 142 ms; RSS 370 to 430 MB |
| `HIVE_VECTOR_STORE=lancedb`, 5k notes | write 50 notes/s (p95 480 ms), saturated remember p50 0.85 s, RSS 0.65 to 0.76 GB |
| same, 20k notes | write **20 notes/s**, remember p95 2 to 6 s, **3.5 GB of disk**, RSS 0.85 GB (before the fix below) |

LanceDB is built for bulk loading; written one note at a time (as Hive does) it creates two table versions per write and
the library keeps them for 7 days. Hive now cleans versions older than 30 s when it compacts (every 100 writes); that bounds
the disk growth but write latency remains well above SQLite's. **Do not use `HIVE_VECTOR_STORE=lancedb` for a growing
knowledge base**; use PostgreSQL. A `hive backup` archives the `lance/` directory too (when no consistent copy can be
made it says so and a `hive reindex` after the restore rebuilds the vectors).

## Recommendations

**Pick the profile**

| Situation | Use |
|---|---|
| Up to ~10k notes, a team of up to ~25 people | embedded SQLite (the default), 1 GB RAM, 1 to 2 vCPU |
| 10k to 50k notes, but few concurrent agents (about 5 or fewer searching at once) | SQLite still works: 35 to 75 ms per search alone |
| More than ~20k notes and more than ~10 active agents; more than ~50k notes; more than one replica; point-in-time recovery | PostgreSQL + pgvector (`HIVE_PROFILE=external`, `HIVE_DATABASE_URL`; migrate with `hive migrate`) |
| More than ~500k notes | see [BENCHMARK](BENCHMARK.md): PostgreSQL HNSW or a dedicated ANN service |

**Resources (hashing or ONNX embedder, 1 server process)**

| Notes | App memory | App CPU | Database memory | Disk (database + 8 backups) |
|---|---|---|---|---|
| 5k | 0.25 GB (0.6 GB with ONNX) | 1 vCPU | n/a / 0.5 GB | 0.1 GB / 0.2 GB |
| 20k | 0.4 GB (0.7 GB with ONNX) | 1 to 2 vCPU | n/a / 1 GB | 0.4 GB / 0.4 GB |
| 50k | 0.6 GB (0.9 GB with ONNX) | 2 vCPU | n/a / 2 GB | 0.9 GB / 1.0 GB |

(First value SQLite, second PostgreSQL where they differ. These include headroom over the measured RSS; they are
extrapolated beyond 50k.) Writing with the real embedder uses all cores: do the initial import with a few workers, off-hours.

**Back up**

- Keep `HIVE_BACKUP_EVERY_MINUTES` at 15 up to ~20k notes. At ~50k SQLite set 60 and accept an hour of possible loss, or use
  PostgreSQL. Your data-loss window is the interval; your downtime is the restore time above plus a minute.
- Practise a restore at your size: `hive restore` into a scratch environment, and time it.
- LanceDB vectors are not in the archive (see above). PostgreSQL: also enable the platform's point-in-time recovery.

**Watch**

`hive_jobs`, `hive_job_oldest_due_seconds` and `hive_vector_items_missing` on `/metrics` ([OBSERVABILITY](OBSERVABILITY.md)):
a rising oldest-due age or a non-zero missing-vector count that does not return to zero is the sign the server is saturated.
Search p95 above ~250 ms on SQLite at a normal pace means it is time to move.

## Caveats (read these first)

- **One machine, shared**: 4 vCPU, with the load generator and PostgreSQL on the same cores and other work running
  (load average 2 to 7). Latencies are noisy to roughly +-30 %; runs were repeated for the headline numbers. A server on its
  own cores will be faster, a cloud disk and network to a managed database slower.
- **Synthetic text**, hashing embedder: search quality is not measured here ([EVALUATION](EVALUATION.md) does that), and the
  hash vectors are 256-dimensional (the real model is 384). The synthetic corpus has fewer near-duplicates than a real one.
- The agents were simulated with a fixed mix; real traffic with long idle gaps costs less (the server closes idle keep-alive
  connections after 5 s; a client must reconnect, which the load test does).
- The paced PostgreSQL numbers include a concurrent `pg_dump`; PostgreSQL's idle latency was not measured separately.
- PostgreSQL ran with its memory limited to `shared_buffers=512MB`; a larger database wants more.
- Beyond 50k notes everything here is extrapolation.

## Run it on your own deployment

```bash
# a scratch copy of the stack: synthetic data, a child server on a free port; nothing real is touched
uv run python scripts/loadtest.py --backend sqlite --notes 5000,20000,50000 --clients 24 \
    --seconds 90 --paced-rate 10 --idle-seconds 20 --out result.json

export HIVE_TEST_PG_DSN="host=/tmp port=55432 dbname=hive_test user=postgres"   # an admin DSN
uv run python scripts/loadtest.py --backend postgres --notes 5000,20000,50000 --seconds 90 --paced-rate 10
```

Options: `--embedder onnx --models-dir DIR` (the real model; ingest is slow), `--vector-store lancedb`, `--paced-only`,
`--backup-during` (an online backup while the paced phase runs), `--no-backup`, `--workdir DIR --reuse` (continue a kept
SQLite data directory), `--keep`. It prints a readable summary per size and writes everything (percentiles, per-third drift,
memory, thread and descriptor counts, disk, job backlog, backup and restore timings, raw samples) to `--out`. The exit code
is 1 if any call failed. PostgreSQL runs create and drop two scratch databases next to the one in the DSN.

To test the sizing of **your** data, ingest a copy of it (`hive ingest`, [INGEST](INGEST.md)) and point your own clients at
it, or raise `--notes` to your expected size and compare the paced latency with your target. For a short regression check
use `HIVE_STRESS_SCALE=1 uv run pytest -m stress tests/stress/test_soak.py` (12 s: errors, latency drift, memory, leaked
threads and descriptors).

## Found while measuring

Fixed (each has a regression test in `tests/regression/test_bug_load_*.py`):

- The SQLite vector scan built and sorted an object for every item in the search space; it now only considers chunks that
  can still reach the top results. A 50k-note search dropped from 173 to 75 ms and the saturated rate rose from 23 to 27
  calls/s at 50k (55 vs 33 at 20k).
- LanceDB kept every table version for 7 days: 3.5 GB of disk for 20k notes.

Open: a saturated SQLite server is limited by the CPU cost of the exact vector scan (more read connections do not help).
