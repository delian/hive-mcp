# Architecture

This document explains how hive-mcp is built: the process, the layers, the data model, the main flows, the security
design, the operational design, and the decisions behind them. It describes the code as it is. Where a behaviour is
specified rather than merely implemented, the requirement is in [REQUIREMENTS.md](REQUIREMENTS.md); exact tables,
constants, routes and settings are in [REQUIREMENTS-ANNEX.md](REQUIREMENTS-ANNEX.md).

## 1. What the system is

hive-mcp is a **remote MCP server that acts as the shared memory of a team**. AI coding agents (Claude Code, Cursor, VS Code,
claude.ai, ChatGPT, Gemini) call tools such as `kb_consult` and `kb_remember` over HTTP; people reach the same knowledge from
Slack, Microsoft Teams, a small admin web UI and a command line. Knowledge is **private to its author until shared**; sharing
beyond a project needs an approver.

```
 AI agents ──MCP/HTTP──┐                                   ┌── SQLite file + blobs (default)
 (Claude Code, Cursor, │      ┌────────────────────────┐   ├── PostgreSQL + pgvector (external or embedded)
  claude.ai, ChatGPT)  ├─────▶│  hive process (ASGI)   │──▶├── optional LanceDB vectors
 Slack  ──slash/events─┤      │  one container         │   └── backup share (archives, lease file)
 Teams  ──bot/invoke───┤      │  tools · services ·    │
 Browser ─admin UI─────┤      │  ports/adapters · jobs │──▶ optional: embedding API, language model,
 CLI / hooks / CI ─────┘      └────────────────────────┘     SMTP / webhook, Google / Entra / OIDC,
                                                              GitHub / Drive / Confluence / Jira / Notion / Graph
```

Everything runs **in one process** (one container): the HTTP server, the tools, the background jobs, the scheduled
connectors, the weekly digest, retention, self-backup, and (optionally) a PostgreSQL child process. There is no queue,
worker, cache or scheduler service to deploy.

## 2. Design principles

1. **Ports and adapters.** Services depend on small interfaces (`ports/`), never on a database. Three storage backends
   (memory, SQLite+filesystem, PostgreSQL+pgvector) implement the same ports and must pass **one conformance suite**.
2. **One place for the visibility rules.** `domain/access.py` is the only definition of who sees what; every adapter
   translates it to its query language and the conformance suite checks they agree. The repository is the authority: an
   index hit is only a candidate and is re-checked before it is shown.
3. **Identity only from the token.** No tool takes a user or organisation argument. The caller is a `Principal` built from
   verified token claims.
4. **Private by default, org isolation absolute.** `org_id` is a hard tenant boundary in every query.
5. **Fail safe, degrade gracefully.** Embedding outage → keyword search; indexing failure → durable retry job; a broken
   connector listing never retires documents; a model failure falls back to heuristics.
6. **Small, strictly typed files.** Source files target 8 KB (hard limit 16 KB), mypy strict, ruff, one module one job.
7. **Proof over promises.** Backups are read back; migrations are proved by comparing checksums of both stores; mutation
   testing guards the security-critical modules; fuzz and stress tests guard parsers and concurrency.

## 3. The process

`hive serve` → `ops/serve.py: run_server(settings)`:

1. (single-container mode) acquire the **lease** in the backup directory (one writer at a time), start the **embedded
   PostgreSQL** if configured, **restore** the newest valid backup if the disk is empty.
2. Build the ASGI app (`app.py: create_app`) and run it under uvicorn wrapped by a draining server.
3. On SIGTERM: readiness fails for `drain_seconds`, in-flight requests finish, background loops are cancelled (the backup
   loop takes a **final verified backup**), services close, then the stop callback stops the embedded PostgreSQL and
   releases the lease. Order matters: nothing writes after the final backup, the database stops after it, the lease is
   released last.

### 3.1 The ASGI application

`create_app` builds the MCP SDK's Streamable-HTTP Starlette app (`/mcp`, stateless, JSON responses) and extends it:

| Piece | Where | Notes |
|---|---|---|
| MCP endpoint + OAuth metadata | `mcp_api/server.py` (`build_mcp`) | token verifier is the static-token verifier, or the OAuth provider when Google/Entra/OIDC sign-in is configured |
| Our routes first | `app.router.routes[0:0] = extra_routes` | so `/revoke` shadows the SDK's |
| OAuth routes | `security/oauth/routes.py` | authorize, token, register, revoke, callback, login pages |
| Admin UI | `webui/` under `/admin` (opt-in) | server-side sessions, CSRF, note page `/admin/n/<id>` |
| Slack / Teams | `slack/routes.py`, `teams/routes.py` | always mounted; each request checks the stored (admin-managed) settings |
| Legal pages | `legal.py` | `/legal/privacy`, `/legal/terms` |
| Health / metrics | `ops/health.py`, `ops/metrics.py` | `/healthz`, `/readyz`, `/metrics` (optional token) |
| Middleware | outermost `ObserveMiddleware` (request ids, metrics, JSON logs), then `RateLimitMiddleware` | rate rules per path family and per token |

The lifespan creates the `Services` container (`bootstrap.py`), starts the job runner and the background loops (on the request loop, or with `HIVE_BACKGROUND_THREAD` in a thread of their own with their own storage connections, `ops/background.py`), and marks
the process ready.

### 3.2 Background work (all in-process)

| Loop | Purpose | Multi-replica safety |
|---|---|---|
| `JobRunner` (`services/runner.py`) | claims durable jobs (indexing, promotion apply, notifications) with leases; exponential backoff 30 s → 1 h | job leases in the database |
| `purge_loop` | purge expired KV rows, session-age retention, retention rules | idempotent |
| `usage_loop` | flush per-person tool-call counters into the audit trail | additive |
| `digest_loop` | weekly digest email per person | claims in KV: once per person per week |
| `group_sync_loop` | Workspace/Entra group → teams/projects/approvers | all-or-nothing apply |
| `connector_loop` | scheduled imports | `put_if_absent` claim per connector per interval slot |
| `backup_loop` | periodic verified archive, final archive at shutdown | lease: one instance |

## 4. Layers and packages

```
src/hive_mcp/
  domain/        pure rules: Item, Scope, Visibility, Tier, Principal/Role, access, tags, history, promotion, retention, errors, clock
  ports/         Protocols the services depend on (items, indexes, kv, jobs, audit, blobs, edges, history, promotions, maintenance, embeddings, rerank, notify)
  adapters/      memory/ · sqlite/ (+fs/ blobs) · postgres/ · lance/ (optional vectors)
  search/        ranking (RRF fusion, weights, freshness), tokenizer, snippets, vector math
  services/      behaviour: knowledge, consult, promotions, sessions, graph, entities, curation, duplicates, assist, staleness,
                 digest, retention, purge, activity, quota, signals, feedback, indexing, ingest, bulk_ingest (resumable folder jobs), notifications, runner, admin
  ingest/        extraction (PDF, Office, HTML, CSV, JSON, text, code, OCR), chunker, URL fetch (SSRF-safe), git reader, folder walker
  embeddings/    hashing (dev), ONNX local, OpenAI-compatible hosted, reranker, model registry
  llm/           optional language model providers (OpenAI-compatible, Anthropic)
  connectors/    scheduled imports: urls, github, drive, confluence, jira, notion, sharepoint + the sync loop
  security/      tokens, directory, principals, sanitize, redact, injection, oauth/ (provider, tokens, login, pages), groups/
  mcp_api/       the MCP surface: tools (grouped by area), prompts, resources, instructions, error mapping
  webui/         admin pages (promotions, audit, usage, activity, people, tokens, integrations, connectors, stale, duplicates, note page)
  slack/ teams/ integrations/   chat front doors and their run-time settings store
  notify/        email / webhook / Slack DM / Teams DM notifiers
  ops/           serve, lease, backup, autobackup, embedded_pg, migrate/, drain, health, metrics, observe, ratelimit, usage, loops
  evaluation/    retrieval and assist evaluation harnesses
  export/        `hive export`: paged JSONL / Parquet dump of items and links with a checksummed manifest
  app.py app_front.py bootstrap.py config.py config_extras.py legal.py cli*.py
clients/hooks/  the slim `hive-hooks` package: Claude Code hooks, editor MCP entries, `init`, session summaries
```

Dependency direction: `mcp_api`, `webui`, `slack`, `teams`, `cli*` → `services` → `ports` ← `adapters`; `domain` is imported by
everyone and imports nothing. `bootstrap.py` is the only place that knows every concrete class.

## 5. Domain model

| Concept | Definition |
|---|---|
| **Item** (a note) | `id` (uuid hex), `org_id`, `kind` (note, research, formula, measurement, decision, best_practice, doc, howto, session, compaction, entity), `title`, `body`, `scope`, `visibility`, `status` (draft, active, superseded, archived), `author_id`, `tags`, `revision`, `supersedes`, `source`, `content_hash`, timestamps |
| **Scope** | `(level, owner)`; levels PRIVATE(0) < PROJECT(1) < TEAM(2) < ORG(3); owner = user id / project id / team id / org id |
| **Visibility** | `scope` (only members of the scope) or `org` (members see it as *primary*, everyone else in the org as *referenced*); a private item can never be org-visible |
| **Principal** | `id`, `org_id`, `role` (VIEWER < MEMBER < EDITOR < ADMIN), `teams`, `projects`, `approves` (scopes they approve into) |
| **Tier** | PRIMARY (inside one of the caller's scopes) or REFERENCED (org-shared from elsewhere; shown labelled EXTERNAL) |
| **Promotion** | a request to move an item to a wider scope/visibility; pending → approved/rejected/withdrawn/stale; one pending per item |
| **Edge** | typed link between items (related, supersedes, contradicts, derived_from, …) |
| **Version** | every earlier revision, written in the same transaction as the change; readable only by someone who could read it at that revision |
| **Job** | durable background task with lease, attempts, backoff |
| **KV record** | small JSON with optional TTL; used for OAuth state, integration settings, connector state, confirmations, one-time claims |
| **Audit event** | append-only record of every state change (and usage counters) |

### 5.1 Access rules (`domain/access.py`)

```
ScopeFilter(org_id, member_scopes)
  tier(acl):  acl.org_id != caller.org           → invisible
              acl.scope ∈ member_scopes           → PRIMARY
              acl.visibility is ORG               → REFERENCED
              otherwise                            → invisible
member_scopes = {private:<me>, org:<my org>} ∪ {project:p} ∪ {team:t}
can_create:  role ≥ MEMBER and member of the scope
can_write :  private → only the owner; other scopes → author, or role ≥ EDITOR
sharing   :  self-service up to project/scope visibility; team/org scope or org visibility → approval
approval  :  ADMIN anywhere in the org; MEMBER+ only for scopes listed in `approves`
```

## 6. Storage

### 6.1 Ports

`ItemRepository` (CRUD with optimistic revisions, idempotency, access-filtered reads, system reads for jobs), `TextIndex`,
`VectorIndex`, `JobQueue`, `AuditLog`, `KeyValueStore` (put, put_if_absent, get, take, delete, purge_expired),
`BlobStore` (content-addressed per org), `EdgeRepository`, `ItemHistory`, `PromotionStore`, `Maintenance` (purge, erase a
person, set status), `Embedder`, `Reranker`, `Notifier`.

### 6.2 Adapters

| Adapter | Items/history/etc. | Text search | Vectors | Blobs |
|---|---|---|---|---|
| memory | dicts (reference implementation, tests) | token index | brute force | dict |
| SQLite (default) | one file, one serialised connection run off the loop, WAL | FTS5 (`unicode61`), access columns denormalised | float32 BLOBs per chunk, brute force over the access-filtered set (or LanceDB with ANN) | filesystem, `<org hex>/<aa>/<sha256>`, atomic writes |
| PostgreSQL | psycopg pool, one transaction per operation, advisory locks | `tsvector` + GIN | pgvector `halfvec` + HNSW per embedding model | `blobs` table (shared by replicas) |

Index rows carry a **copy of the access rules** (`org_id, scope_level, scope_owner, visibility`); `move` updates them
(`sync_acl`). Schema migrations are forward-only; the app refuses a database newer than it understands.

### 6.3 One conformance suite

`tests/conformance` runs the same behavioural tests against every backend (`memory`, `sqlite`, `postgres` when
`HIVE_TEST_PG_DSN` is set). A new adapter is accepted when it passes unchanged.

## 7. Main flows

### 7.1 `kb_remember`
`mcp_api` tool → `KnowledgeService.remember`: sanitise (remove hidden characters, **mask secrets**, flag prompt-injection
phrasing with the `flag/injection` tag) → check sharing policy → quota check → idempotency lookup → repository create (item,
tags, revision 1, history) in one transaction → audit event → index inline (`IndexService.index_or_enqueue`: keyword index
first, then embeddings; any failure queues a durable job and the call still succeeds, reporting `indexed: false`).

### 7.2 `kb_consult` (search)
1. Keyword search (access-filtered) and, if the query has terms, vector search with the embedder's `min_similarity`
   floor (outage → note "semantic search unavailable").
2. Weighted **reciprocal rank fusion**: keyword top-5 weight 1, vector weight 2, `k = 60`.
3. Re-fetch candidates through the repository (access re-check; the index is not trusted).
4. Multiply by `scope_weight × (referenced 0.6) × status_weight × (0.5 + 0.5 × freshness)` and, if enabled, a +2% nudge for
   notes confirmed in the last 90 days; freshness halves every `half_life_days[kind]` (sessions 30 d … formulas never).
5. Optional cross-encoder rerank of the top candidates; snippets; origin labels; graph links (supersedes/contradicts).
6. Results split into primary and referenced (EXTERNAL); referenced are added in AUTO mode only when too few primary matched, minus the ones the caller hid with `kb_ignore` (`services/foreign_records.py`). `services/foreign.py` holds fork / diff-upstream / suggest-edit; its records live in the key-value store (`foreign:*` namespaces, listed with `KeyValueStore.scan`).

### 7.3 Promotion
`kb_request_promotion` stores a request bound to the item's current revision. `kb_decide_promotion` by an approver is
atomic (`pending → approved`), enqueues an `apply_promotion` job; the job re-checks that the item is unchanged (otherwise
the request becomes **stale**), moves the item (scope + indexes) and notifies the requester. Approvers are notified by
email/webhook/Slack/Teams according to configuration.

### 7.4 Sessions and agent hooks
Claude Code hooks (`hive-hooks`): SessionStart injects the workflow and the project's top notes; UserPromptSubmit searches and
injects up to three relevant notes; PreCompact and SessionEnd call `kb_report_compaction` / `kb_report_session`. Hooks never
fail or block the agent (short deadlines, silent when unconfigured). A `session` label links the notes consulted during a
session (`kb_session_sources`).

### 7.5 Ingestion and connectors
`IngestService`: extract (size/zip-bomb/XML-entity safe) → chunk → items with source and tags → idempotent by content hash.
`ConnectorSync` keeps the knowledge base equal to a source: new → ingest; changed (version marker or content fingerprint) →
ingest again and archive the previous items; vanished → archive, **only** if the whole listing succeeded and not more than
half (of ≥5 documents) vanished at once. State per connector lives in the KV store.

### 7.6 Retention and erasure
Rules (`kind`/`scope`, `days`, `purge|archive`) run in `purge_loop`; age counts from the last change. `forget` erases a
person's private notes (and, optionally, everything they wrote) and **pseudonymises** them (`erased-<hex>`) in audit,
history, promotions and edges.

### 7.7 Backup, restore, migration
`create_backup`: consistent snapshot (SQLite online backup API or `pg_dump`), blobs, tokens, users, secret key, manifest
with SHA-256s, written to `.part` then renamed; `verify_backup` reads it back. Self-backup runs when something changed
(file mtimes, or Postgres row-activity counters) and keeps the newest N. `hive migrate` copies every table between SQLite and
PostgreSQL through a neutral row form and **proves** the copy by reading both stores back and comparing counts and an
order-independent checksum per table; it never writes to the source.

## 8. Security architecture

| Concern | Design |
|---|---|
| Authentication | static bearer tokens (SHA-256 hashes in `tokens.json`, shown once, expiry, revocation, per-token rate limit); OAuth 2.1 authorization server for interactive assistants (PKCE, dynamic client registration, single-use codes with replay revocation, rotating refresh tokens with reuse detection and token **families**), identity from Google / Entra ID / generic OIDC; a user **directory** decides who may sign in and what they belong to |
| Authorisation | Principal from claims only; unknown role = read-only; tokens of a person stop working when the person is removed/disabled; every query filtered by org + scope; approvals per scope |
| Admin UI | server-side sessions (signed id cookie, server holds state), CSRF token + Origin check on every POST, strict CSP, no scripts, `SameSite=Lax`, `path=/admin`; a return-cookie only ever points at a note page |
| Chat front doors | Slack: v0 HMAC signature, 5-minute window, one-time signatures; Teams: Bot Framework JWT (RS256, issuer, audience = app id, `serviceurl` claim equals the activity's), service-URL allow-list for replies, tenant check; users matched by work email; channel answers quote only org-wide + channel-project knowledge; secrets of integrations sealed with AES-GCM (HKDF from the server secret key, field name as AAD) and never returned |
| Content safety | secrets masked before storage; hidden Unicode removed; prompt-injection heuristics flag (never silently reject); output escaping per channel (mrkdwn, Teams markdown, HTML); SSRF-safe URL fetch (off by default); archive extraction without path traversal, size/entry limits; XML without entities |
| Privacy toward providers | text goes to an embedding/LLM provider only at or above a configured scope (`embedding_min_scope`, `llm_min_scope`; private notes stay home by default) |
| Abuse limits | quotas per person; rate limits per client address (auth routes), per token (MCP); registration limit |
| Audit | append-only; every write, promotion, login, export, delete and integration change; counts per person |
| Errors | not-found and not-visible are indistinguishable; internal errors are opaque; logs never contain secrets |

## 9. Configuration architecture

Three layers, later wins: defaults in `Settings` (`config.py` + `config_extras.py`, pydantic-settings, `HIVE_*`) →
environment → **run-time integration settings** stored in the database (`IntegrationStore`, namespace `integration:config`)
for Slack and Teams, editable by admins (page `/admin/integrations` or admin tools) with secrets sealed. Settings validators
reject illegal combinations at startup (for example embedded Postgres together with a database URL; replicas > 1 on SQLite).

## 10. Deployment topologies

| Topology | Storage | Durability | When |
|---|---|---|---|
| **A. Local / one container** | SQLite in `/data` volume | the volume (`hive backup` for archives) | personal, small team |
| **B. One container in a cloud with a disposable disk** | SQLite on local disk | `HIVE_BACKUP_DIR` on a durable share: verified archives every N minutes, final archive at stop, restore on empty disk, lease, revision hand-off | Azure Container Apps (`deploy/azure/single.bicep`), Cloud Run |
| **C. One container with embedded PostgreSQL** | `WITH_POSTGRES` image, cluster in `/data/pg` | same as B using `pg_dump` archives | bigger knowledge base without a second service |
| **D. External PostgreSQL, several replicas** | Postgres + pgvector | the database | multi-replica (`HIVE_REPLICAS>1`, shared rate limits, fixed secret key) |

## 11. Quality architecture

| Layer | What it guarantees |
|---|---|
| Unit and service tests | behaviour of domain and services on memory + SQLite (+ Postgres) |
| Conformance suite | every adapter agrees on one contract |
| Integration tests | real HTTP server with fakes of Google, Slack, Teams, GitHub…; real MCP client |
| Fuzz tests (Hypothesis) | parsers and text checks never raise unexpected errors and stay fast |
| Mutation testing (mutmut) | 2,100+ mutants in the security-critical modules all killed (weekly CI job) |
| Regression tests (`tests/regression`) | one test per bug found in review, named `BUG-<area>-<n>` |
| Stress tests (`tests/stress`, marker `stress`) | concurrency, scale and adversarial-input bounds; `HIVE_STRESS_SCALE` enlarges them |
| CI | ruff, mypy strict, file-size guard, pytest with a pgvector service, dependency audit, image build + smoke tests (plain, single-container, embedded Postgres), Trivy scan, Bicep build |

## 12. Extension points

* **A storage backend**: implement the ports, register a factory in `tests/conftest.py`, pass the conformance suite.
* **A connector**: a class with `documents() -> AsyncIterator[Document]` (raise when the listing is incomplete), a spec entry in
  `connectors/spec.py` (`TYPES`, `REQUIRED`) and a factory branch.
* **A tool**: add a function in the matching `mcp_api/tools_*.py` with `@guarded`, `READ`/`WRITE` annotations, and call a
  service with `current_principal()`; document it in `kb://guide` instructions if agents should know about it.
* **An embedder or language model**: implement `Embedder` / `LlmProvider`, add a branch in the factory, declare its
  `min_similarity` and the default minimum scope that may be sent to it.
* **A chat front door**: verify the platform's signature/JWT, resolve identity by email, answer with channel-appropriate
  escaping, and keep settings in `IntegrationStore`.

## 13. Trade-offs and known limits

* SQLite is single-process; multi-replica needs PostgreSQL. A hard crash in mode B loses at most the changes since the last
  archive (default 15 minutes); planned stops lose nothing.
* Brute-force vector search over the access-filtered set is exact and simple; beyond a few hundred thousand notes use
  PostgreSQL/pgvector or LanceDB.
* The embedded PostgreSQL is not supervised: if it dies the readiness check fails and the platform restarts the container.
* Chat integrations and cloud connectors are verified against fakes and the platforms' documented formats, not against the
  real services.
* Ranking weights were tuned on a small developer-written evaluation set; measure on your own notes (`hive eval`).
