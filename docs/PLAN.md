# Swarm Knowledge MCP Server — Development Plan

Working name: **hive-mcp** (placeholder). Python, remote-first MCP server that stores, catalogs, searches
and shares team knowledge (session logs, compactions, research, formulas, measurements, docs) between agents.

> **Status (current):** phases 0-6 and most of 7 are implemented (see the README documentation table for each
> feature). This document is the original design plus revisions; where it disagrees with the feature pages, the
> pages win. Differences worth knowing: the project package is `hive_mcp` (not `hive`); storage is SQLite + filesystem
> or Postgres + pgvector (DuckDB/LanceDB were not adopted); the rerank, graph, entity, history, admin and web pages are built.
> **Still open:** real Google/assistant/Azure verification, Workspace group sync, multi-replica readiness (quota cache,
> rate limits and job runner assume one process), OCR for scanned documents, a LanceDB adapter, newest-first audit paging.

---------------------------------------------------------------------------------------------------

## 1. Research findings that shape the design

| # | Finding | Consequence for the design |
|---|---------|----------------------------|
| 1 | MCP spec **2026-07-28** removed protocol sessions (`Mcp-Session-Id`), the GET stream, and `Last-Event-ID` resumability. Each request is a self-contained POST; server→client asks use "multi round-trip requests" (MRTR). Change notifications go over a `subscriptions/listen` stream. | A modern client is bound to no worker, so **soft upgrades are mostly free**. A broken stream loses only that in-flight request, so **every write tool must be idempotent** and every long job must be a **background job with a job id** (no long streams). |
| 2 | Spec 2025-03-26…2025-11-25 clients still use sessions, GET streams, and resumption. | We must serve **both eras**. Python SDK v2 does this with the same app. 2025-era clients need session stickiness or `stateless_http=True`. After a restart they get a 404 for the unknown session and re-initialize ("asked to reconnect"). |
| 3 | Python SDK **v2** is the current stable line (`mcp` 2.x, Python ≥3.11). The server class is `MCPServer` (renamed from `FastMCP`). Transport options moved from the constructor to `run()` and the app builders (`streamable_http_app()`); the app is mounted into Starlette/FastAPI. The lifespan runs once at startup. Sealed `request_state` keys default to one per process, so multi-replica deployments need `RequestStateSecurity(keys=[...])`. | Pin `mcp>=2,<3`. Build the ASGI app ourselves so we control middleware (auth, Origin check, request ids, drain). |
| 4 | Spec requires **Origin validation** (403 on bad Origin) and "proper authentication". The Python SDK's `mcp.server.auth` implements an **OAuth 2.1 resource server** (`TokenVerifier`, RFC 9728 protected-resource metadata, 401 + `WWW-Authenticate`). | v1: bearer tokens via our own `TokenVerifier`. v2: external IdP (OIDC) using the same verifier interface. |
| 5 | DuckDB is **single-process read/write**. Many processes may read only if nobody writes. Multi-process writes need Quack or DuckLake+Postgres. Appends never conflict; two writers on the same row do. | Old and new server versions **cannot both hold the DB read-write** during an upgrade. The design needs a "storage owner" decision (section 6.2). |
| 6 | DuckDB `vss` (HNSW): persistence is **experimental** (`hnsw_enable_experimental_persistence`), WAL recovery is incomplete (crash can corrupt the index), the whole index is rewritten at each checkpoint, it must fit in RAM, deletes are only marked (needs `hnsw_compact_index`), and the index is used only for `ORDER BY distance LIMIT k`. | Treat the HNSW index as a **rebuildable cache**, never the source of truth. Store vectors as plain `FLOAT[n]` columns; exact scan to ~100k–500k chunks, which is fast enough. |
| 7 | DuckPGQ (SQL/PGQ graph) is a **community extension**, still in development, and pinned to older DuckDB releases. | Do **not** depend on it. Model the graph as `entity`/`edge` tables and traverse with recursive CTEs. Keep a `GraphRepository` interface so a real graph engine can be swapped in later. |
| 8 | Hybrid retrieval (BM25 + dense vectors fused with RRF, optional cross-encoder rerank) is the standard best practice; RRF k≈60 is the default, tune later with labelled queries. Chunks of ~500–1000 tokens with overlap; prefer semantic/structural boundaries. | Implement hybrid + RRF behind a `Ranker` interface. Keep a small golden-query eval set from day 1. |
| 9 | Agent-memory systems (Graphiti/Zep, Mem0, Letta, Cognee) converge on: separate episodic (raw sessions) vs semantic (distilled facts) memory, **temporal/bitemporal facts** (supersede, don't delete), scoped memory, and an extraction policy. Shared multi-agent memory is not native in most of them. MCP only standardises the interface, not the memory model. | Own our data model: **episodes → distilled knowledge items → entities/edges**, with `valid_from/valid_to` and `supersedes`. Do not adopt Neo4j/Graphiti as a dependency in v1. |

> Items marked **[verify]** below were not confirmed in primary sources during research and must be checked in Phase 0.

Sources: [MCP Streamable HTTP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http) ·
[MCP transports 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) ·
[Python SDK v2 docs](https://py.sdk.modelcontextprotocol.io/) ·
[SDK authorization](https://py.sdk.modelcontextprotocol.io/authorization/) ·
[DuckDB vss](https://duckdb.org/docs/2.0/core_extensions/vss.html) ·
[DuckDB concurrency](https://duckdb.org/docs/2.0/connect/concurrency.html) ·
[DuckDB graph queries](https://duckdb.org/docs/current/guides/sql_features/graph_queries) ·
[Agent memory comparison](https://vectorize.io/articles/mem0-vs-zep) ·
[Hybrid search/RRF](https://dev.to/ji_ai/reciprocal-rank-fusion-why-k60-buries-your-best-hit-525c) ·
[Gunicorn signals](https://gunicorn.org/signals/)

---------------------------------------------------------------------------------------------------

## 2. Key honest caveats (read first)

1. **A server cannot make an agent consult it.** MCP is pull-based. "Automatic" behaviour comes from three
   levers, all of which we ship: (a) the server `instructions` string and well-written tool descriptions,
   (b) **client hooks** (Claude Code `SessionStart`, `PreCompact`, `Stop/SessionEnd`, `UserPromptSubmit`)
   generated by `hive hooks install`, and (c) a `CLAUDE.md`/`AGENTS.md` snippet generated by the CLI.
2. **Sharing implicitly is a data-leak risk.** Secrets, credentials, customer data and personal data will
   end up in session logs. Redaction + visibility defaults + review queue are P0, not nice-to-have.
3. **Server-side LLM summarising is not assumed.** Sampling support in clients is uneven. Reorganising /
   summarising is done **by the calling agent** through propose/apply tools; the server provides the
   candidates, dry-run, audit log and undo. An optional server-side LLM provider can be added later.
4. **DuckDB's single-writer rule conflicts with literal zero-downtime upgrades** (section 6.2).

---------------------------------------------------------------------------------------------------

## 3. Architecture

```
 Agents (Claude Code, other MCP clients)          Humans / CI
        │  MCP Streamable HTTP (2025 + 2026 eras)        │  `hive` CLI (same service layer)
        ▼                                                ▼
┌───────────────────────── transport / app (ASGI) ───────────────────────────┐
│ middleware: Origin check · auth (TokenVerifier) · request-id · drain · rate │
│ mcp_api/: tools · resources · prompts   (thin: validate → call service)     │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               ▼
┌──────────────────────── services (all business logic) ──────────────────────┐
│ KnowledgeService · SearchService · TagService · GraphService · IngestService │
│ ReorgService · SessionService · AccessPolicy · Redactor · AuditLog           │
└───────┬───────────────┬───────────────┬──────────────────┬──────────────────┘
        ▼               ▼               ▼                  ▼
  Repositories     EmbeddingProvider  Ranker(RRF)      JobQueue (background)
  (ports)          (port; local ONNX  (BM25+vec+graph  ingest, embed, reindex,
  DuckDB adapter    default, remote   +scope distance) reorg apply
  [Postgres later]  optional)
```

### 3.1 Package layout (`src/hive/`, every file ≤ 8 KB)

```
hive/
  config.py            # pydantic-settings, one source of truth
  domain/              # pure dataclasses/enums, no I/O: item, scope, tag, entity, edge, job, errors
  storage/             # base.py (connection, tx, retry), migrations/, items_repo.py, tags_repo.py,
                       # chunks_repo.py, graph_repo.py, jobs_repo.py, audit_repo.py
  embeddings/          # base.py (Protocol), local.py, remote.py, cache.py
  ingest/              # scanner.py, parsers/{md,text,code,pdf,csv}.py, chunker.py, pipeline.py
  search/              # bm25.py, vector.py, graph_expand.py, fusion.py, scope_distance.py, ranker.py
  services/            # one module per service
  security/            # auth.py, access_policy.py, redact.py, rate_limit.py
  mcp_api/             # app.py (build ASGI app), tools_*.py, resources.py, prompts.py, schemas.py
  cli/                 # main.py (typer), ingest_cmd.py, tags_cmd.py, hooks_cmd.py, export_cmd.py, admin_cmd.py
  ops/                 # health.py, drain.py, migrate.py, metrics.py
tests/ unit/ integration/ contract/ eval/   docs/
```

### 3.2 "Write it once" rules (bug-surface control)

- **One** `ScopeFilter` / `AccessPolicy` builds the SQL predicate for visibility; every repository query and
  every tool goes through it. No hand-written visibility `WHERE` anywhere else.
- **One** `Repository` base (connection handling, transactions, conflict retry, parameter binding). No
  string-formatted SQL. Only bound parameters. Identifiers come from enums.
- **One** tag normaliser (`normalize_tag`) used by tools, CLI, ingest and import.
- **One** content-hash/idempotency function shared by ingest, `remember`, and import.
- **One** `Ranker` pipeline; BM25-only, vector-only and hybrid are configurations, not separate code.
- Tools are declared in a **table** (name, schema, service method, required permission). Registration,
  docs and the contract tests are generated from that one table.
- CLI commands and MCP tools call the **same service methods** (no logic in either).
- File-size guard: `scripts/check_file_size.py` (fails CI and pre-commit if any `.py` > 8192 bytes; ≈ 150–200
  lines). Ruff has no byte-size rule, so this is a tiny custom check.

---------------------------------------------------------------------------------------------------

## 4. Data model and cataloguing

### 4.1 Core entities

| Entity | Purpose | Key fields |
|--------|---------|-----------|
| `episode` | Raw, append-only: session log, compaction summary, tool output, pasted file | id, kind, body, author, agent, client_session_ref, project, created_at, content_hash |
| `item` | Distilled knowledge (note, research, formula, measurement, decision, best_practice, doc, how-to) | id, kind, title, body, summary, status (draft/active/superseded/archived), confidence, scope, visibility, author, source (episode/file/url), `valid_from`, `valid_to`, `supersedes`, revision |
| `chunk` | Searchable pieces of items/docs | item_id, ord, text, token_count, embedding `FLOAT[n]`, embedding_model_id |
| `tag` / `item_tag` | Faceted catalogue | namespace/name, aliases, parent, description, assigned_by (human/agent/rule), confidence |
| `entity` / `edge` | Knowledge graph | entity(type, canonical_name, aliases); edge(src, predicate, dst, item_id provenance, weight, valid_from/to) |
| `source_file` | Ingested files | path/uri, sha256, mtime, parser, last_scanned, status |
| `job`, `audit_log`, `principal` | Ops/security | |

### 4.2 Cataloguing best practice (faceted, controlled vocabulary)

- Use **facets**, not one big tag list: `kind/` (formula, measurement, research…), `domain/` (programming,
  radar, rf, mechanical, pm…), `project/…`, `team/…`, `lifecycle/` (draft, validated, deprecated),
  plus free-form `topic/…`.
- Controlled vocabulary with **aliases/synonyms** (SKOS-style): `rf` ≡ `radio-frequency`. Agents that invent a
  near-duplicate tag get it mapped to the canonical one (and told so in the tool response).
- `kb_suggest_tags(text)` returns existing tags ranked by similarity first. New tags are allowed but flagged
  `proposed` until accepted (or auto-accepted by policy).
- **Retagging** = batch operation with dry-run, per-item diff, audit log and one-call undo.
- **Dedupe/versioning:** content hash for exact duplicates; embedding similarity above a threshold returns
  "possible duplicate of X" at write time. Updates create a new revision, never destructive overwrite.
- **Bitemporal facts:** contradicting information supersedes (sets `valid_to`, `supersedes`) rather than
  deleting, so "what did we believe in March?" works.

### 4.3 Scope hierarchy and "relative distance"

Scopes: `private(user) < project < team < company < public/general`. Each item has a scope + visibility; each
principal has memberships. Retrieval takes a **context** (user, project, team, company; inferred from token
and/or passed by the hook from the working directory).

```
score = rrf(bm25, vector, graph)                       # topical relevance
        × scope_weight(distance(item.scope, context))  # project 1.0 → team 0.85 → company 0.7 → general 0.5 [tunable]
        × freshness(item.updated_at, kind)             # formulas/standards decay slowly, session logs fast
        × (0.7 + 0.3·confidence) × status_penalty      # superseded/deprecated down-ranked, not hidden
```

Weights live in config and are validated by the eval set, not guessed.

---------------------------------------------------------------------------------------------------

## 5. MCP surface

**Server `instructions`** (short, imperative): "Before starting a task, call `kb_consult`. After finishing
research or discovering reusable facts, call `kb_remember`. At session end call `kb_report_session`."

### 5.1 Tools (all take/return typed Pydantic models; reads are `readOnlyHint`, writes carry `idempotency_key`)

| Group | Tools |
|-------|-------|
| Consult | `kb_consult(task, context)` (one-call "give me the best relevant knowledge", compact, with citations), `kb_search(query, filters, mode, limit, cursor)`, `kb_get(id, revision?)`, `kb_related(id)` |
| Write | `kb_remember(kind, title, body, tags, scope, visibility, idempotency_key)`, `kb_report_session(summary, log, outcomes)`, `kb_report_compaction(summary)`, `kb_update(id, patch)`, `kb_supersede(id, new)` |
| Catalogue | `kb_list_tags(facet?, prefix?)`, `kb_suggest_tags(text)`, `kb_tag(ids, add, remove)`, `kb_retag_plan(filter, rules)` → `kb_apply_plan(plan_id)` |
| Graph | `kb_link(src, predicate, dst)`, `kb_neighbors(entity, depth, predicates)`, `kb_entity_search(q)` |
| Ingest | `kb_ingest_path(uri|text, tags, scope)` → returns `job_id`; `kb_job_status(job_id)` |
| Curate (agent-driven) | `kb_reorg_candidates(scope)` (clusters of near-duplicates, stale items, untagged items) → agent proposes summaries/merges → `kb_apply_plan(plan_id)` (dry-run default, audited, undoable via `kb_undo(audit_id)`) |
| Share | `kb_export(filter, format)`, `kb_import(bundle)` (JSONL + Parquet bundle, with provenance preserved), `kb_set_visibility(ids, …)` |

Design rules: small, high-signal responses (token budget param, snippets not whole docs, `next_cursor`);
actionable error messages ("tag 'rf-stuff' mapped to 'domain/rf'"); stable tool names; versioned schemas.

### 5.2 Resources and prompts

- Resources: `kb://item/{id}`, `kb://tags`, `kb://project/{id}/digest` (a rolling "what the team learned
  this week" page that clients can pin).
- Prompts: `consult-before-task`, `end-of-session-report`, `reorganize-knowledge`, `research-and-store`.
- Notifications (2026 era) via `subscriptions/listen`: `resources/updated` for the project digest;
  `tools/list_changed` after a server upgrade adds tools.

### 5.3 Making agents use it automatically (client side, shipped by the CLI)

`hive hooks install --client claude-code` writes: `SessionStart` hook → inject top-N project digest;
`UserPromptSubmit` → optional light `kb_consult`; `PreCompact` → send the compaction through
`kb_report_compaction`; `Stop/SessionEnd` → `kb_report_session`. Plus an `AGENTS.md` snippet. All hooks are
thin wrappers calling the same HTTP API, fail open (never block the user if the server is down), and have
timeouts.

---------------------------------------------------------------------------------------------------

## 6. Storage, search, ingestion

### 6.1 Retrieval stack

- **Lexical:** DuckDB `fts` (BM25). **[verify]** the FTS index is a static snapshot that must be rebuilt after
  data changes; plan an incremental "delta table + periodic rebuild" or a SQLite-FTS5 side index if too slow.
- **Dense:** local ONNX embedding model through `EmbeddingProvider` (default small multilingual model,
  e.g. via fastembed **[verify]** licence/size/offline), remote providers optional. Store `embedding_model_id`
  per chunk so models can be swapped and re-embedded as a job.
- **Graph expansion:** from top hits, 1–2 hops over `edge` (recursive CTE) to pull related formulas/decisions.
- **Fusion:** RRF (k=60 start) → scope/freshness/confidence multipliers → optional cross-encoder rerank.
- Chunking: structure-aware (headings for markdown, functions/classes for code via tree-sitter, rows/blocks
  for CSV/measurements), 500–1000 tokens with overlap, with parent-title prepended to each chunk.

### 6.2 Storage engine decision (needs your input — see section 11)

| Option | Pros | Cons |
|--------|------|------|
| **A. DuckDB single storage-owner process** (recommended for v1) | Simple, fast, embeddable, great Parquet export/analytics, one file to back up | Single writer ⇒ upgrades are "drain → restart" (seconds), not hot-swap. |
| B. A + separate long-lived **store daemon** (rarely upgraded) with stateless API replicas in front | True rolling upgrades of the MCP layer | More moving parts; store protocol to design/secure |
| C. **Postgres + pgvector + FTS** (+ edge tables) | Multi-writer, true blue/green, mature backup/HA, scales to many users | Heavier ops; less "embeddable"; lose DuckDB analytics (can still export to Parquet/DuckDB) |

Plan: build against `storage/` ports so A→C is an adapter swap, not a rewrite. Start with A.

### 6.3 Ingestion

- CLI: `hive ingest ./docs --tags domain/radar --scope team --watch` and config file `hive.toml`
  (`[[sources]] path, include, exclude, tags, scope, schedule`).
- Pipeline: scan → hash (skip unchanged) → parse → chunk → embed → extract entities/edges (rule-based: headings,
  code symbols, formula notation, links; LLM-assisted extraction via the agent later) → upsert → audit.
- Idempotent, resumable, incremental (sha256 + mtime), deletions mark `archived` instead of hard-deleting.
- Server-side ingest only reads from **allow-listed roots** (path traversal / SSRF guard); remote agents push
  content as text/bundles rather than asking the server to read arbitrary paths.
- Parsers sandboxed with size/time limits (PDF parsing is a classic crash/DoS vector).
- Export/import: JSONL (items, tags, edges) + Parquet for bulk; `hive export --scope project/x` for sharing
  between servers; import preserves ids/provenance and is idempotent.

---------------------------------------------------------------------------------------------------

## 7. Security, tenancy, privacy

- Auth: v1 static/hashed bearer tokens mapped to a `principal` (user, teams, projects, role). v2 OAuth 2.1
  resource server with external OIDC IdP via the SDK's `TokenVerifier` + RFC 9728 metadata (needed for claude.ai
  custom connectors).
- TLS terminated at a reverse proxy; Origin allow-list; request size limits; per-principal rate limits.
- **Identity is taken from the token, never from tool arguments** (an agent must not be able to claim another
  author/team). Scope/visibility are checked server-side on every read and write.
- **Redaction on write:** secret scanners (API keys, tokens, private keys, `.env` patterns) + optional PII
  patterns; hits are blocked or masked and reported to the caller. Default visibility for agent-written session
  logs is `private`; promotion to team/company is explicit (or policy-based after redaction + review queue).
- **Prompt-injection hygiene:** stored content is untrusted data. Tool results wrap retrieved text with
  provenance and never contain server-generated "instructions". Retrieved content can't call tools.
- Audit log for every write/retag/delete/export; soft-delete + retention policy; GDPR-style per-user purge.

---------------------------------------------------------------------------------------------------

## 8. Soft upgrades without losing clients

1. **Protocol layer:** serve 2026-07-28 (stateless) and 2025-era clients from the same app (SDK v2).
   2026 clients: any instance can answer, so restarts are invisible apart from in-flight requests.
   2025 clients: run `stateless_http=True` where their features allow; otherwise they get 404 on the old session
   and re-initialize (the spec-defined "reconnect").
2. **Graceful drain:** on SIGTERM/SIGHUP: readiness → "draining" (health endpoint 503 so the proxy stops routing),
   finish in-flight requests with a bounded timeout, flush job state, checkpoint DB, exit. Run with
   uvicorn graceful shutdown or Gunicorn + `uvicorn-worker` (HUP = start new workers then gracefully stop old).
   Reverse proxy retries idempotent requests on connection reset.
3. **Idempotent writes:** every write tool takes `idempotency_key` (stored with result) so a retry after a
   broken stream is safe. Reads are naturally safe.
4. **Long work = jobs:** ingest/reindex/reorg run in a persisted job queue, resumed after restart; clients poll
   `kb_job_status`. No dependence on a long-lived connection.
5. **Schema migrations:** versioned, forward-only, **expand → migrate → contract**; the app refuses to start on
   a newer DB than it knows (prevents downgrade corruption); automatic pre-migration backup/snapshot.
6. **API compatibility:** never rename/remove a tool in a minor release; add optional fields only; contract
   tests compare the generated tool schema to a committed snapshot; breaking changes go through a deprecation
   window and `tools/list_changed`.
7. **Data upgrades:** embedding model change = new `embedding_model_id` + background re-embed; searches use
   the model whose coverage is complete, so quality never drops mid-migration. HNSW/FTS indexes are rebuilt
   into a shadow object and swapped.
8. **Storage-owner caveat:** with option A the DB handover is "drain → checkpoint → start new". Target downtime
   is seconds; with option B/C it can be zero. Decide in Phase 0.

---------------------------------------------------------------------------------------------------

## 9. Engineering standards

- **uv** (lockfile committed), Python 3.12, `src/` layout, `pyproject.toml` only.
- **ruff** (lint + format; enable `E,F,I,B,UP,SIM,ASYNC,S,PL,RUF,TCH,ANN`), **mypy --strict** (or pyright
  strict), pydantic v2 models at all boundaries, no `Any` in public signatures.
- **pytest** + `pytest-asyncio`, `hypothesis` for tag normalisation / chunking / scope predicate, `pytest-cov`
  (gate ≥ 85%, 95% for `security/` and `search/fusion`), contract tests for the tool schemas, integration tests
  that boot the real ASGI app and talk to it with the SDK client in both `legacy` and modern modes, plus a
  restart-during-request test.
- **pre-commit**: ruff, mypy, file-size guard, secret scan, `uv lock --check`.
- **CI**: lint → types → unit → integration → eval (retrieval quality must not regress) → build image.
- **Docs**: mkdocs-material; ADRs in `docs/adr/`; tool reference generated from the tool table; operator runbook
  (backup/restore, upgrade, rollback).
- **Observability**: structured logs with request id, OpenTelemetry traces, Prometheus metrics (latency, queue
  depth, search hit-rate, empty-result rate).
- Container image with non-root user, pinned base, healthcheck, volume for data.

---------------------------------------------------------------------------------------------------

## 10. Phased roadmap

| Phase | Goal | Deliverables | Exit criteria |
|-------|------|--------------|---------------|
| **0. Foundations (≈1 wk)** | Remove unknowns | Repo skeleton (uv, ruff, mypy, pytest, pre-commit, file-size guard, CI); spike: SDK v2 app serving both eras + bearer auth; spike: DuckDB FTS rebuild cost, exact-vector scan at 100k/500k chunks, vss as cache; embedding model choice; decide storage option A/B/C; ADRs | Spikes documented; numbers recorded; decisions made |
| **1. Core store (≈2 wk)** | Remember & recall | Domain model, migrations, repositories, `kb_remember/get/update/search` (BM25 + vector + RRF), tags v1, idempotency keys, audit log, Redactor, AccessPolicy | Round-trip works from a real MCP client; ≥85% coverage; golden-query eval baseline |
| **2. Agent integration (≈1–2 wk)** | Agents use it automatically | Server instructions, `kb_consult`, session/compaction reporting, resources/prompts, `hive hooks install` for Claude Code, AGENTS.md snippet | A fresh agent session consults and reports without being told |
| **3. Catalogue & curation (≈2 wk)** | Keep knowledge tidy | Faceted tag service with aliases, `kb_suggest_tags`, retag plans (dry-run/apply/undo), dedupe on write, `kb_reorg_candidates`, supersede/bitemporal | Retag 1k items with undo; duplicates detected |
| **4. Ingestion & graph (≈2–3 wk)** | Files and relations | CLI ingest + watch + `hive.toml`, parsers (md/text/code/csv/pdf), chunker, job queue, entities/edges, graph expansion in ranking, export/import bundles | Ingest a real project folder incrementally; graph-boosted search beats baseline on the eval set |
| **5. Multi-user & remote hardening (≈2 wk)** | Team swarm | Principals/teams/projects, scope-distance ranking, visibility promotion workflow, rate limits, OAuth 2.1 resource server, TLS/proxy reference config | Two users on two machines share knowledge with correct isolation (security tests) |
| **6. Soft upgrades & ops (≈1–2 wk)** | Never lose clients | Drain/readiness, graceful restart scripts, job resume, migration safety (backup, version guard), schema snapshot contract tests, restart-during-load test, runbook | Upgrade under load: no failed non-in-flight requests; in-flight retried safely |
| **7. Scale & polish** | | Cross-encoder rerank, Postgres adapter (if chosen), metrics dashboards, digest generation, docs site | |

---------------------------------------------------------------------------------------------------

## 11. Proactive bug-risk register

| Risk | Mitigation |
|------|-----------|
| Visibility leaks (SQL predicate forgotten) | Single `AccessPolicy`/`ScopeFilter`; every repository method requires a `Principal`; property tests that a principal never sees out-of-scope rows through search, graph, export, resources |
| SQL injection | Bound parameters only; identifier allow-lists; ruff `S608` on |
| Duplicate writes after retried/broken stream | `idempotency_key` + unique constraint; content-hash dedupe |
| Stale / corrupt indexes (HNSW, FTS) | Indexes are derived & rebuildable; consistency check job; exact-scan fallback if index missing |
| DuckDB write conflicts / lock errors | Single writer process; short transactions; bounded conflict retry in the base repository |
| Embedding model change → mixed vector spaces | `embedding_model_id` per chunk; never compare across models |
| Tag explosion / near-duplicate tags | Normaliser, aliases, suggest-first, `proposed` status |
| Prompt injection via stored text | Provenance wrapping, no server-generated instructions in data, review of agent-written shared items |
| Secrets/PII shared with the swarm | Redactor on write + on ingest + on import; private-by-default |
| Huge responses blow client context | Token budgets, snippets, pagination |
| Path traversal / SSRF in ingest | Allow-listed roots, no remote URL fetch by default, resolved-path checks |
| Parser crashes/DoS | Size/time limits, subprocess isolation for PDF/office parsers |
| Clock/time zone bugs in bitemporal data | UTC everywhere, injected clock in services for tests |
| Sealed `request_state` breaks across replicas/restarts | Configure shared `RequestStateSecurity(keys=[...])` from config |
| 2025-era client session loss on restart | Document + test the reconnect path; prefer stateless mode |
| Upgrade runs on newer DB then rolls back | Version guard + pre-migration snapshot + expand/contract |
| Files drift above 8 KB | CI guard; split by responsibility, not arbitrarily |

---------------------------------------------------------------------------------------------------

## 12. Decisions I need from you

1. **Scale and team shape:** how many users/agents and how much data (hundreds of docs or millions of chunks)?
   This decides DuckDB (A) vs Postgres (C).
2. **Zero-downtime strictness:** is "a few seconds of restart, clients auto-retry" acceptable, or must upgrades
   be truly zero-gap (then B or C)?
3. **Clients:** Claude Code only, or also claude.ai connectors / other agents? (OAuth 2.1 is needed for
   claude.ai custom connectors.)
4. **Embeddings:** must everything stay on-prem/offline (local model), or may a hosted embedding API be used?
5. **Sharing policy:** is agent-written knowledge shared with the team by default (with redaction), or private
   until someone promotes it?
6. **Hosting:** where will it run (single VM + Docker, Kubernetes, cloud container service)?
7. **8k limit:** do you mean 8 KB of bytes per file (my assumption, ≈150–200 lines)?

---------------------------------------------------------------------------------------------------

## 13. Revision 2 — decisions taken (supersedes 6.2, parts of 7 and 8, and section 12)

### 13.1 Decisions
| Topic | Decision |
|-------|----------|
| Scale | Up to ~100 GB. Treated as a **hard architectural input** (see 13.2) |
| Upgrades | Restart of a few seconds is OK **provided no client has to be restarted** (drain + stateless 2026 path + idempotent retries; 2025-era clients re-initialize automatically) |
| Clients | claude.ai, ChatGPT, Gemini, DeepSeek-style web assistants **and** coding agents / pipelines ⇒ public HTTPS endpoint + OAuth 2.1 (Authorization Code + PKCE + Dynamic Client Registration / CIMD) **and** static API tokens for pipelines/CLI |
| Embeddings | Pluggable: offline local ONNX **and** hosted API; per-chunk `embedding_model_id`; config chooses per deployment/scope (a private/offline scope can forbid hosted providers) |
| Sharing | **Private by default; explicit promotion**; items can be tagged to groups/projects; cross-group *read-through* with explicit provenance (13.4) |
| Hosting | Docker container on Azure/GCP, private network, exposed through a tunnel; same image runs locally via docker compose |
| File size | See 13.5 |

### 13.2 Storage change: DuckDB is no longer the primary store
100 GB makes the earlier DuckDB single-file design a poor fit: one writer process, vss HNSW must fit in RAM and
is experimental, and the earlier "exact scan to 500k chunks" assumption fails (100 GB of text is tens of millions
of chunks; 384-dim float32 vectors alone would be tens of GB).

**New default (Option C, from section 6.2): PostgreSQL as system of record**
- Postgres (managed: Azure Database for PostgreSQL Flexible Server / Cloud SQL / AlloyDB; local: container) with
  `pgvector` (HNSW, `halfvec`/binary quantization to shrink RAM) for dense search and Postgres full-text
  search (`tsvector`/GIN, or ParadeDB `pg_search` for true BM25) for lexical; graph as `entity`/`edge` tables with
  recursive CTEs (Apache AGE is optional later). One database means transactional consistency between items, tags,
  ACLs, vectors and jobs, and **multi-writer ⇒ real rolling/blue-green upgrades are possible** (a bonus over what you required).
- **Raw files/blobs in object storage** (Azure Blob / GCS / local MinIO), referenced by sha256; Postgres holds
  metadata, chunks, vectors. Much of the "100 GB" is likely raw files, not vectors: **please confirm** (vectors
  are typically 10–40% of the text size depending on dimension/chunking).
- **DuckDB stays, but as an analytics/export tool**: Parquet exports, offline analysis, local single-user
  "lite" mode (`storage=duckdb`) for dev and tests. Same repository ports, two adapters. The test suite runs
  against both so the abstraction cannot rot.
- Escape hatch if pgvector tail latency/recall disappoints at tens of millions of vectors: Qdrant behind the
  same `VectorIndex` port. The only large-scale public benchmark found is vendor-run (pgvectorscale beating
  Qdrant on throughput, Qdrant better on p99), so **Phase 0 must benchmark with our own dims, filters and recall target**.
- Do not assume Postgres sizing from blogs: estimate = chunks × (dim × 2 bytes for halfvec + overhead) + HNSW (~1.5–2×).

### 13.3 Remote access and auth (cloud LLMs through a tunnel)
- Common denominator across claude.ai / ChatGPT / Gemini: public **HTTPS** + Streamable HTTP MCP endpoint +
  `401 + WWW-Authenticate` → RFC 9728 protected-resource metadata → OAuth 2.1 AS metadata → DCR/CIMD + PKCE.
  Third-party sources only for ChatGPT and Gemini specifics **[verify against OpenAI/Google official docs in Phase 0]**;
  each vendor uses its own redirect URL/client identity, so the AS must allow-list per client.
- **Do not build an OAuth authorization server ourselves.** Use an external IdP that supports DCR/CIMD and OIDC
  (Keycloak, Authentik, Zitadel, Auth0/Okta, or a thin AS gateway in front of Entra ID, which does **not** support DCR
  natively). The MCP server is only the *resource server* (SDK `TokenVerifier`). Principals, groups and projects
  come from token claims (or SCIM sync), never from tool arguments.
- Pipelines/CLI: scoped, revocable API tokens (hashed in DB, per-principal, per-scope, expiry).
- **Tunnel choice:** Tailscale Funnel works (443/8443/10000, tailnet domain only, TLS only) but has
  non-configurable bandwidth limits and unknown SSE/idle behaviour; **Phase 0 spike: run a streaming MCP call and a
  large ingest through Funnel**. Alternatives to compare in the same spike: Cloudflare Tunnel (+ Access/WAF/rate
  limit), or simply an Azure/GCP load balancer or Front Door/Cloud Armor with the container in a private subnet.
  Recommendation: Funnel for dev/test and small teams; a managed ingress with WAF + rate limiting for production,
  because this endpoint is public and holds company knowledge. Bulk ingest should NOT go through the LLM-facing
  endpoint: use a separate authenticated ingest API/CLI path or direct blob upload.
- Origin validation, per-principal rate limits, request-size caps, and Cloud-LLM IP/identity logging remain mandatory.

### 13.4 Groups, projects and cross-group reference
Model: `principal ∈ groups ∈ orgs`, `item.owner_scope` (private → project → group/team → org → general) plus
`item.shared_with` ACL entries. Rules:
1. Private by default. `kb_promote(ids, to_scope)` is explicit, audited, redaction re-run, and may require
   approver in `team` and above (review queue).
2. **Retrieval has two tiers**, never silently merged:
   - *Primary* = items in the caller's own scopes (ranked with scope-distance weighting).
   - *Referenced* = items from other groups/projects the caller is **allowed** to see (published/general or
     shared_with), returned only when primary results are weak or when asked (`include_foreign=auto|always|never`).
3. Every result carries `origin: {scope, group, project, author, promoted_at, status}` and a human-readable
   `provenance` line (e.g. "from group *RF-Lab* / project *Radar-X*, promoted by …"), so the agent must tell
   the user where it came from. Foreign items are labelled `external`.
4. User options on a foreign item: **adopt-and-extend** (`kb_fork(id, into_scope)` creates a local item with
   `derived_from` link and upstream revision; later `kb_diff_upstream` shows upstream changes), **suppress for
   this project** (`kb_ignore(id, project)` stored per project so it stops surfacing), or **propose
   improvement upstream** (`kb_suggest_edit` → owner review queue).
5. Foreign access is enforced in the single `AccessPolicy` (Postgres row-level security as defence in depth).
   A foreign item's text is never merged into local items without the fork step.

### 13.5 File size limit
8 KB (~200 lines) is a good *target* but too strict as a hard rule: SQL migrations, pydantic schema modules and
tests would be split artificially, which increases cross-file coupling instead of reducing bugs.
Recommendation: **target ≤ 8 KB, hard CI limit 16 KB for `src/`** (fails the build), warn at 12 KB;
tests/generated/migration files exempt via explicit allow-list (reviewed). 20 KB is too loose to stay "readable
in one screen-scroll pass", 16 KB (~400 lines) is the upper bound where one module still has one responsibility.

### 13.6 Plan changes by phase
- **Phase 0** gains: Postgres+pgvector benchmark at 1M/10M/50M synthetic chunks (halfvec, HNSW params, filtered
  queries with ACL predicates, hybrid fusion); storage-adapter conformance tests; Funnel vs Cloudflare vs cloud LB
  streaming spike; confirm ChatGPT/Gemini/claude.ai connector requirements from official docs; IdP choice
  (Keycloak vs managed); dim/size capacity model; confirm the 100 GB breakdown.
- **Phase 1** uses the Postgres adapter first (DuckDB lite adapter second, shared conformance suite).
- **Phase 5** adds IdP integration, groups/projects, promotion, referenced tier, fork/ignore/suggest-edit.
- **Phase 6** upgrades: with Postgres, blue/green or rolling (two versions can overlap) and the migration rule
  "expand → migrate → contract" becomes mandatory; drain-and-restart remains the simple fallback and local mode.
- **Deployment deliverables:** Dockerfile (multi-stage, non-root), `docker compose` (app + postgres + minio +
  keycloak + tunnel profile) for local, Terraform/Bicep examples for Azure and GCP, backup/restore (PITR for
  Postgres, versioned blob bucket), runbook.

### 13.7 New risks
| Risk | Mitigation |
|------|-----------|
| Cloud LLM vendors differ in OAuth/DCR quirks | Conformance spike per vendor, keep AS external, log failed handshakes with vendor/client id |
| Public endpoint abuse or exfiltration | WAF + per-principal rate limits + response size caps + audit; read-only tokens for LLM connectors by default |
| Foreign-knowledge leakage via "reference" tier | Referenced tier only from explicitly promoted/shared items; property tests across groups; RLS |
| Hosted embedding API receiving private/sensitive text | Per-scope policy: private/sensitive scopes forced to local model; redaction before send; provider allow-list |
| Mixed embedding models across scopes | Separate vector index per `embedding_model_id`; query embeds with each active model or routes by scope |
| pgvector memory blowup at ~50M+ vectors | halfvec/binary quantization + re-rank, partition by org/scope, fallback Qdrant port |
| Bulk ingest through the tunnel | Separate ingest path, resumable multipart upload, job queue |

---------------------------------------------------------------------------------------------------

## 14. Revision 3 — answers to the open questions

| Question | Answer | Consequence |
|----------|--------|-------------|
| What the ~100 GB is | Mixed / unknown | Size for the worst case (all text embedded) and measure in Phase 0. Capacity model covers both; blobs always go to object storage, so the worst case only affects Postgres/vector sizing. |
| First cloud | **Azure** | Azure Container Apps (or AKS) + Azure Database for PostgreSQL Flexible Server (pgvector enabled) + Blob Storage + Key Vault + Front Door/WAF for ingress. Stay cloud-neutral in code (Docker, Postgres, S3-style blob port with Azure Blob adapter), GCP later as a Terraform/Bicep variant. |
| Identity | **Google Workspace** users, hosted on Azure | Google alone does not provide the dynamic client registration the cloud LLMs need. Use a **self-hosted authorization server with DCR/CIMD (Keycloak or Zitadel) that federates login to Google Workspace** (OIDC upstream). Users sign in with Google; the AS issues the MCP tokens; the MCP server only verifies them. Groups/projects are managed in the AS (or synced from Workspace groups) and arrive as token claims. Phase 0 spike: Keycloak + Google federation + one real claude.ai connector handshake. |
| Promotion | **Approver required** for team level and above | `kb_promote` creates a *pending promotion*; scope owners approve/reject in a review queue (CLI + MCP tools `kb_review_list/approve/reject`). Private → project may stay self-service by config; redaction re-runs at submit and at approval; all steps audited. |

Updated Phase 0 checklist:
1. Repo skeleton (uv, ruff, mypy strict, pytest, pre-commit, 16 KB file guard, CI).
2. SDK v2 app serving 2025+2026 clients with bearer auth; restart-during-request test.
3. Postgres+pgvector benchmark (1M/10M/50M chunks, halfvec, ACL-filtered hybrid queries).
4. Keycloak + Google federation + DCR handshake with claude.ai; check ChatGPT/Gemini official docs.
5. Tunnel streaming test: Tailscale Funnel vs Azure Front Door.
6. Capacity model for worst-case 100 GB; decide vector dimension/model.
7. ADRs for storage, auth, tunnel, embeddings.

---------------------------------------------------------------------------------------------------

## 15. Revision 4 — single-container deployment (supersedes the compose/multi-service parts of 13.6 and 14)

### 15.1 Requirement
One Docker image, one container, started the same way on a laptop (`docker run`), on a VM, or as an Azure
container. No required sidecars (no separate Postgres, MinIO, Keycloak, tunnel container).

### 15.2 How: one image, two storage profiles chosen by config (same code, same tests)
| Profile | Components inside the one container | Use |
|---------|-------------------------------------|-----|
| **`embedded` (default)** | Python ASGI app + embedded stores on one mounted volume `/data`: relational/FTS in **SQLite or DuckDB**, vectors in an **embedded ANN store** (LanceDB-style on-disk index, or sqlite-vec for small), blobs on the **local filesystem**, **built-in OAuth authorization server** + job worker as in-process tasks | Local, tests, single-team Azure/VM, up to the low tens of GB |
| **`external`** | Same container, but `DATABASE_URL` (Postgres+pgvector), `BLOB_URL` (Azure Blob/S3) and `OIDC_ISSUER` point to managed services | 100 GB scale, HA, rolling upgrades |
Rule: **the container never *requires* anything else; external services are optional upgrades.** Everything
else (tunnel, TLS) is optional config:
- Tunnel/TLS: built-in optional Tailscale (`TS_AUTHKEY` starts `tailscaled` in userspace mode, Funnel on 443) or run
  behind Azure Container Apps ingress / Front Door which already gives HTTPS. Cloudflare tunnel stays an external option.
- Multiple processes in the container only where unavoidable (app + optional tailscaled), managed by a tiny init
  (`tini` + a small Python supervisor), not a full supervisord stack.

### 15.3 Consequences for the design
1. **Storage ports are now mandatory, not optional**: `RelationalStore`, `VectorIndex`, `BlobStore`, `JobQueue`,
   `AuthProvider`. Two adapter sets (embedded / external) run against one shared conformance suite in CI.
   This keeps duplicated logic out: services never know which profile is active.
2. **Built-in authorization server** (replaces "always an external IdP"): the app itself implements the OAuth
   2.1 AS endpoints cloud LLMs need (metadata, DCR/CIMD, authorize + PKCE, token, refresh, revoke) and delegates
   *login* to Google Workspace via OIDC (upstream). Must reuse a vetted library (e.g. Authlib) or the SDK's server-side
   auth provider interface; **do not hand-roll crypto/token logic**. **[verify]** which of these the SDK v2 still
   offers. In `external` profile it can be disabled in favour of Keycloak/Entra-gateway. Security tests for this
   module are P0 (redirect URI allow-listing per vendor, PKCE enforcement, token binding to resource, refresh rotation).
3. **Single writer again** in `embedded` profile ⇒ upgrades there are drain-and-restart of seconds (accepted).
   `external` profile keeps overlapping versions possible. Same container image, so the upgrade runbook has
   two sections.
4. **Config via env vars + one `hive.toml`** (12-factor); secrets via env/Key Vault reference, never baked in.
   `hive serve --profile embedded|external`, plus `hive doctor` to verify volume, permissions, DB version, ports.
5. **First-run bootstrap**: container creates `/data`, runs migrations, generates signing keys, and prints a
   one-time admin enrollment link. Idempotent on restart.
6. **Image**: multi-stage, non-root, read-only root FS with `/data` and `/tmp` writable, healthcheck
   (`/healthz` liveness, `/readyz` readiness incl. drain), embedded local embedding model optionally baked in
   (`hive:full`) or downloaded to `/data/models` on first run (`hive:slim`) so the image stays small and
   offline use is possible.

### 15.4 Azure caveat (important, verify in Phase 0)
Azure Container Apps and Container Instances persist volumes mainly through **Azure Files (SMB/NFS)**.
SQLite/DuckDB/embedded ANN and file locking on SMB shares are a known source of corruption and slowness; NFS is
better but still must be tested. Therefore:
- Single container on a **VM or AKS with a managed disk** (block storage): `embedded` profile is safe.
- Azure Container Apps / ACI with Azure Files: use `embedded` only for dev/demo or after the Phase 0
  lock/fsync stress test passes; for production use `external` (managed Postgres + Blob) from the same image.
This is a limitation of the platform's storage, not of the design.

### 15.5 Plan changes
- **Phase 0** adds: embedded-store benchmark (LanceDB vs sqlite-vec vs DuckDB-exact at 1M/10M chunks, disk-based
  ANN, filtered queries); Azure Files vs managed disk fsync/lock stress test; built-in AS spike with a real
  claude.ai connector and Google upstream login; Tailscale-in-container userspace test; image size budget.
- **Phase 1** builds the conformance suite and **both** adapter sets from the start (embedded first, external second).
- **Phase 6** upgrade runbook per profile; `docker run` smoke test and Azure deploy (Bicep for Container Apps and for
  VM) in CI; scripted "start locally in one command" quickstart (`docker run -v hive:/data -p 8080:8080 hive`).
- Section 13.2 stays valid as the `external` profile; the "Postgres is the default" statement is replaced by
  "Postgres is the scale profile".
