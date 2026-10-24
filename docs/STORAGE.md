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
