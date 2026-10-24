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
| Postgres + pgvector + object storage (external profile) | `adapters/postgres` | Next |
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
fingerprint, ignored viewer permission, no-op ACL update, unquoted FTS terms, non-UTC timestamps, cross-org blob access.
All are caught. Two early gaps (time-zone test data that accidentally sorted correctly; FTS quoting reachable only
through the tokenizer) were fixed by sharper tests.
