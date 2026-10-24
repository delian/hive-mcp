# Services

`hive_mcp/services` combines the storage ports into behaviour. Services take the authenticated `Principal` on every call;
nothing here trusts identity from tool arguments.

| Service | Responsibility |
|---|---|
| `KnowledgeService` | `remember`, `get`, `update`, `tag`, `move` (promote/demote). Redacts secrets, enforces the sharing policy, audits, keeps indexes in step |
| `ConsultService` | Hybrid retrieval: BM25-style keyword + semantic search, rank fusion, access re-check, ranking by scope/status/age, snippets, origin labels |
| `SessionService` | Session and compaction reports: searchable summary + full log stored as a redacted file; `get_log` |
| `IndexService` | Text + vector indexing. Failures queue a durable `index_item` job instead of losing the item |

## Rules worth knowing
- **Private by default.** `remember` without a scope stores in the caller's private space. Self-service sharing stops at project
  level with `Visibility.SCOPE`; team/org scope or org-wide visibility needs an approver (admins). Promotion requests and the
  approval queue are not built yet: until then, members get a clear `PermissionDenied`.
- **Secrets are masked before storage** (titles, bodies, patches, session logs). Redaction is best-effort; the result lists
  what kind of secret and how many, never the value.
- **Origin labels.** Each result says where it came from. Results from groups the caller does not belong to are
  `EXTERNAL`, listed separately, and added in `AUTO` mode only when the caller's own knowledge has fewer than `min_primary` matches.
- **The repository is the authority.** Index hits are only candidates; every hit is re-checked through the repository, so a
  stale or wrong index can never reveal an item.
- **Relevance floor.** Vector search always has nearest neighbours, so each embedder declares `min_similarity`; weaker hits are dropped.
- **Indexes carry a copy of the access rules.** `move` updates both indexes (`sync_acl`). Index jobs read the current item with
  `get_system(org_id, id)` and never delete anything for an item that is not found in the org.
- **Embedding outages degrade gracefully.** The keyword index is written first; the item stays stored and a job retries the
  embedding; `consult` falls back to keyword search and says so in `notes`.

## Not built yet
Promotion requests / approver queue, retag plans, dedupe-on-write, entity/edge graph, ingestion pipeline, job runner process,
real embedding models (local ONNX / hosted), the MCP tools and OAuth wiring.
