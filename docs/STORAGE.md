# Storage interfaces

Services depend only on the ports in `hive_mcp.ports`; backends live in `hive_mcp.adapters`. The in-memory adapter
(`adapters/memory`) is the executable specification. Every other adapter (SQLite/LanceDB for the `embedded` profile,
Postgres/pgvector + Blob for `external`) must pass the same suite in `tests/conformance` — add it to `BACKENDS`
in `tests/conformance/conftest.py` and run `uv run pytest tests/conformance`.

## Ports
| Port | Responsibility |
|---|---|
| `ItemRepository` | System of record: create (idempotent), get, get_many, update (optimistic revision), relocate (promotion), list (keyset pages) |
| `TextIndex` | Keyword search, BM25-style, Unicode case-insensitive, one hit per item |
| `VectorIndex` | Cosine search per embedding model; `replace` swaps all chunks of an item atomically |
| `BlobStore` | Content-addressed files, namespaced per org |
| `JobQueue` | Durable jobs with leases, fenced completion, retries and a dead state |
| `AuditLog` | Append-only events with a per-store sequence number |

## Access control (one implementation)
`domain/access.py` is the only place visibility is decided.
- Tenant boundary first: `org_id` must match.
- `PRIMARY`: the item's scope is one of the caller's scopes (own private space, projects, teams, the org).
- `REFERENCED`: the caller is not a member but the item is `Visibility.ORG`. Results carry this tier so answers can say
  where knowledge came from.
- Private items can never be org-visible (`check_visibility`); they must be promoted (`relocate`) first.
- Writes: viewers never; members edit their own; editors/admins edit anything in scopes they belong to; referenced items are read-only.
- Missing and invisible items both raise `NotFound`, so existence never leaks.
Indexes denormalise the item's `Acl`; after any `relocate` the service must call `update_acl` on both indexes
(conformance tests cover the index side; the service layer must be tested for the call).

## Invariants the conformance suite pins down
- Idempotent create: same key + payload replays, different payload raises; a failed create does not consume the key.
- Updates need the current revision (`Conflict` otherwise); tag changes are add/remove sets so concurrent edits keep each other's tags.
- Pagination never repeats or skips, even with identical timestamps; forged cursors are rejected.
- Vector `replace` is all-or-nothing: bad vectors (zero, NaN, wrong dimension) leave the previous state untouched; models are never mixed.
- A job whose lease expired is reclaimed; the old worker can no longer complete, fail or heartbeat it; crash loops end `DEAD`.
- Text search is case-insensitive for every script (e.g. Bulgarian), unlike SQLite `LIKE`.

## Adapters
| Adapter | Package | Status |
|---|---|---|
| In-memory | `adapters/memory` | Reference implementation and test double |
| SQLite (embedded profile) | `adapters/sqlite` + `adapters/fs` | Items, FTS5 text search, exact vector search, jobs, audit; blobs on the filesystem |
| Postgres + pgvector (external profile) | `adapters/postgres` | Done; blobs are rows in a `blobs` table (shared by replicas, covered by pg_dump) |
| LanceDB vectors (scalable embedded vectors) | `adapters/lance` | Planned; SQLite vectors are exact and fine up to about 200k chunks (about 130 ms) |

### SQLite specifics
- One serialised connection, WAL mode, each operation one transaction run off the event loop; "database is locked"
  becomes `StorageBusy` (a retryable `HiveError`), never a crash.
- Forward-only migrations by `PRAGMA user_version`; opening a database from a newer version is refused.
- Text is tokenised in Python with the shared tokenizer and stored in FTS5, so SQLite and every other backend match alike.
  Query terms are always quoted before they reach FTS5.
- The SQL access filter (`acl_where`) is only a prefilter; the tier is always decided by `ScopeFilter.tier`.
  A property test checks the two agree.
- Timestamps are stored as UTC ISO text so text order equals time order, whatever zone the caller used.
- Blobs: per-org directory named by the hex of the org id (no path tricks), atomic write, hash verified on read.

## How the tests were checked
Besides the conformance suite, deliberate bugs ("mutants") were injected into the adapters to prove the tests catch them:
dropped tenant filter, removed revision check, stale vector chunks, removed job-lease fencing, ignored idempotency
fingerprint, ignored viewer permission, no-op ACL update, unquoted FTS terms, non-UTC timestamps, cross-org blob access
(SQLite/memory), and for Postgres: dropped tenant filter, missing advisory locks, shallow vector search, missing lease
fencing, ignored pagination cursor, unquoted tsquery, unmapped data errors, unlocked migrations, no-op vector ACL update.
All are caught. Early gaps (time-zone test data that accidentally sorted correctly; FTS quoting reachable only through the tokenizer;
a job de-duplication race that one burst of requests missed) were fixed by sharper tests.

### Postgres specifics
- Needs the `vector` extension (pgvector 0.8+). Migrations create it if the role may; on managed services allow-list it first
  (Azure Database for PostgreSQL: `azure.extensions`). Install the client with `pip install hive-mcp[postgres]`.
- Migrations run in one transaction under an advisory lock, so several app instances can start together; a database from a newer
  version is refused.
- Writes lock only rows the caller can see (`FOR UPDATE` after the ACL filter), so users cannot stall each other's invisible items.
  Idempotent creates and job de-duplication use advisory locks keyed by a hash of the JSON-encoded parts (no separator collisions).
- Jobs are claimed with `FOR UPDATE SKIP LOCKED`; lease fencing as in the other adapters.
- Vectors: one `halfvec` table per embedding model (pgvector needs a fixed dimension per index), HNSW m=32, ef_construction=200
  (from the benchmark), inner product on unit vectors. Scores are cosine similarity accurate to about 1e-3 (float16 storage).
  Search over-fetches chunks and deepens up to four rounds so one item with many chunks cannot crowd out others.
  Deleted rows stay in the HNSW index until it is rebuilt: schedule `REINDEX` after large deletions (maintenance job to come).
- Keyword search uses `tsvector`/GIN over text tokenised by the shared tokenizer, ranked with `ts_rank` (not BM25, and no IDF).
  Ranking quality should be measured on real data; ParadeDB `pg_search` is the upgrade path for true BM25.
- Text must not contain NUL characters (Postgres cannot store them): the domain rejects them for items and idempotency keys,
  and any other driver data error becomes a `ValidationError`.
- Pool exhaustion, deadlocks and lost connections surface as the retryable `StorageBusy`.

### Running the Postgres tests
```bash
# any server with pgvector; the tests create and drop one schema per test
export HIVE_TEST_PG_DSN="host=localhost port=5432 dbname=postgres user=postgres password=postgres"
uv run pytest
```
Without `HIVE_TEST_PG_DSN` the Postgres cases are skipped. CI runs them against the `pgvector/pgvector:pg16` image.
