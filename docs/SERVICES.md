# Services

`hive_mcp/services` combines the storage ports into behaviour. Services take the authenticated `Principal` on every call;
nothing here trusts identity from tool arguments.

| Service | Responsibility |
|---|---|
| `KnowledgeService` | `remember`, `get`, `update`, `tag`, `move`, `history`, `restore`. Redacts secrets, enforces the sharing policy, audits, keeps indexes in step |
| `ConsultService` | Hybrid retrieval: keyword + semantic search, weighted rank fusion, access re-check, weighting by scope/status/age, optional rerank and confirmation nudge, snippets, origin labels, graph links |
| `SessionService`, `SessionSources` | Session and compaction reports (searchable summary + redacted log), `get_log`; which notes a session relied on |
| `IndexService` | Text + vector indexing. Failures queue a durable `index_item` job instead of losing the item |
| `PromotionService`, `NotificationService` | Approval requests, decisions, applying a promotion safely, notifying approvers and requesters |
| `GraphService`, `EntityService` | Typed links between notes; people, systems and concepts as notes other notes can point at |
| `IngestService` | Files, URLs and repositories into notes (extraction, chunking, idempotent by content) |
| `CurationService`, `DuplicateFinder`, `CurationAssist`, `StaleReview`, `RankSignals`, `SearchFeedback` | Tags, merge, near-duplicates, optional language-model merge proposals and contradiction checks, "still true?" review, confirmation nudge, search ratings |
| `DigestService`, `DigestSender`, `ActivityReport` | What is new for a person or project, weekly emails, activity report |
| `RetentionService`, `PurgeService` | Delete/archive by age, erasure of a person, permanent deletion |
| `QuotaGuard`, `AdminService`, `VectorAdmin`, `JobRunner` | Limits per person, audit/usage views, embedding-model housekeeping, background jobs |
| `ConnectorSync` (in `connectors/`) | Keeps the knowledge base equal to an external source |

## Rules worth knowing
- **Private by default.** `remember` without a scope stores in the caller's private space. Self-service sharing stops at project
  level with `Visibility.SCOPE`; team/org scope or org-wide visibility needs an approver (see [PROMOTION.md](PROMOTION.md)).
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
