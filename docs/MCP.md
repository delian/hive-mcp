# MCP interface

Endpoint: `POST {public_url}/mcp` (Streamable HTTP, JSON responses, stateless). Serves both protocol eras (2025-11-25 sessions-style
clients and 2026-07-28 stateless clients) from the same app. `GET /healthz` (liveness) and `GET /readyz` (readiness; 503 while
starting or draining) need no credentials.

## Authentication
Every MCP request needs `Authorization: Bearer <token>`. Missing or invalid tokens get `401` with a `WWW-Authenticate` header
pointing at the OAuth protected-resource metadata, which is what OAuth-capable clients use for discovery.

Today the server verifies **static bearer tokens** (for pipelines, the CLI and coding agents). Interactive assistants
(claude.ai, ChatGPT, Gemini) need the OAuth authorization server, which is not built yet; it plugs into the same
`TokenVerifier` and claim mapping.

```bash
# create a token (printed once; only its hash is stored in <data_dir>/tokens.json)
uv run hive token create --label alice-laptop --subject alice --org acme --role member --team rf --project radar-x
uv run hive token list
uv run hive token revoke alice-laptop      # effective immediately, no restart

uv run hive serve                          # http://localhost:8080/mcp
claude mcp add --transport http hive http://localhost:8080/mcp --header "Authorization: Bearer <token>"
```

Identity comes only from the token (`sub` plus the claims `hive_org`, `hive_role`, `hive_teams`, `hive_projects`). **No tool takes a
user or organisation argument**; extra arguments are ignored. A missing role means read-only; an unknown role also means read-only.

## Tools
| Tool | Purpose |
|---|---|
| `kb_consult(query, limit, kinds, tags, other_groups)` | Search before starting a task. Returns `results` (your scopes) and `from_other_groups` (labelled EXTERNAL) |
| `kb_get(id)` | Read one item in full (body capped at 20,000 characters, `truncated` flag) |
| `kb_remember(title, body, kind, tags, share_with_project, idempotency_key, source)` | Store knowledge, private unless `share_with_project` names one of your projects |
| `kb_update(id, expected_revision, ...)` | Edit with optimistic concurrency; also retags, archives, supersedes |
| `kb_share(id, expected_revision, project)` | Move a private item into a project you belong to |
| `kb_report_session` / `kb_report_compaction` | Record a session or the summary a context was compacted into; optional full log stored as a redacted file |
| `kb_get_log(id)` | Read a stored log |

Prompts: `consult_before_task`, `end_of_session_report`. The server `instructions` tell agents the workflow (consult first,
remember reusable results, report sessions, never store secrets, say when results are external).

## Behaviour agents can rely on
- Errors are readable and never leak internals: not-found and not-visible look identical; stale revisions say to re-read with `kb_get`;
  unexpected failures say only "internal error".
- Retrying writes with the same `idempotency_key` never stores twice.
- Detected secrets are masked before storage and the response says what kind (never the value).
- Writes beyond a project (team/org scope, org-wide visibility) are refused until the approval workflow exists.

## Configuration (environment)
`HIVE_DATA_DIR` (default `/data`), `HIVE_PORT`, `HIVE_PUBLIC_URL` (the externally visible URL, used for Host/Origin checks and
OAuth metadata), `HIVE_PROFILE` (`embedded` | `external`), `HIVE_DATABASE_URL` (external profile), `HIVE_TOKENS_FILE`,
`HIVE_ALLOWED_ORIGINS`, `HIVE_RUN_JOBS`, `HIVE_DRAIN_SECONDS`, `HIVE_SHUTDOWN_TIMEOUT`.
