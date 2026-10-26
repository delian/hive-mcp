# MCP interface

Endpoint: `POST {public_url}/mcp` (Streamable HTTP, JSON responses, stateless). Serves both protocol eras (2025-11-25 sessions-style
clients and 2026-07-28 stateless clients) from the same app. `GET /healthz` (liveness) and `GET /readyz` (readiness; 503 while
starting or draining) need no credentials.

## Authentication
Every MCP request needs `Authorization: Bearer <token>`. Missing or invalid tokens get `401` with a `WWW-Authenticate` header
pointing at the OAuth protected-resource metadata, which is what OAuth-capable clients use for discovery.

The server accepts **static bearer tokens** (pipelines, the CLI, coding agents) and, when Google sign-in is configured,
**OAuth access tokens** for interactive assistants (claude.ai, ChatGPT, Gemini): see [OAUTH.md](OAUTH.md).

```bash
# create a token (printed once; only its hash is stored in <data_dir>/tokens.json)
uv run hive token create --label alice-laptop --subject alice --org acme --role member --team rf --project radar-x
uv run hive token list
uv run hive token revoke alice-laptop      # effective immediately, no restart

uv run hive serve                          # http://localhost:8080/mcp
claude mcp add --transport http hive http://localhost:8080/mcp --header "Authorization: Bearer <token>"
```

Tokens whose subject is an email address belong to a person in the user directory: they stop working the moment that person is
removed or disabled (`hive user remove` / `hive user add --disabled`). Other subjects (service accounts, pipelines) are not people and
are only ended by `hive token revoke` or expiry.

Identity comes only from the token (`sub` plus the claims `hive_org`, `hive_role`, `hive_teams`, `hive_projects`). **No tool takes a
user or organisation argument**; extra arguments are ignored. A missing role means read-only; an unknown role also means read-only.

## Tools
| Area | Tools |
|---|---|
| Core | `kb_consult(query, limit, kinds, tags, other_groups)` search first (results from your scopes plus `from_other_groups`, labelled EXTERNAL; related links and supersede/contradict notes included); `kb_get(id)`; `kb_remember(title, body, kind, tags, share_with_project, idempotency_key, source)` private unless shared; `kb_update(id, expected_revision, ...)`; `kb_share(id, expected_revision, project)` |
| Reports | `kb_report_session`, `kb_report_compaction` (optional full log stored redacted), `kb_get_log(id)` |
| Approvals | `kb_request_promotion`, `kb_list_promotions`, `kb_decide_promotion` - see [PROMOTION.md](PROMOTION.md) |
| Graph | `kb_link`, `kb_unlink`, `kb_graph` - see [GRAPH.md](GRAPH.md) |
| Entities | `kb_entity_save`, `kb_entity_resolve`, `kb_entity_mention` - see [ENTITIES.md](ENTITIES.md) |
| History | `kb_history`, `kb_version`, `kb_diff`, `kb_restore` - see [HISTORY.md](HISTORY.md) |
| Curation | `kb_tags`, `kb_rename_tag`, `kb_retag`, `kb_similar`, `kb_merge`, `kb_list` - see [CURATION.md](CURATION.md) |
| Ingestion | `kb_ingest` (PDF, Office, text, code), `kb_ingest_url` (off by default) - see [INGEST.md](INGEST.md) |
| Deletion | `kb_purge` - see [RETENTION.md](RETENTION.md) |
| Admin | `kb_admin_usage`, `kb_admin_audit` - see [ADMIN.md](ADMIN.md) |

Prompts cover consulting, end-of-session reports, saving context before compaction, tidying a tag and reviewing promotions. Resources: `kb://guide`, `kb://item/{id}`, `kb://recent`.
The server `instructions` tell agents the workflow (consult first, remember reusable results, report sessions, never
store secrets, say when results are external).

## Behaviour agents can rely on
- Errors are readable and never leak internals: not-found and not-visible look identical; stale revisions say to re-read with `kb_get`;
  unexpected failures say only "internal error".
- Retrying writes with the same `idempotency_key` never stores twice.
- Detected secrets are masked before storage and the response says what kind (never the value).
- Writes beyond a project (team/org scope, org-wide visibility) go through an approval request (`kb_request_promotion`).

## Configuration (environment)
`HIVE_DATA_DIR` (default `/data`), `HIVE_PORT`, `HIVE_PUBLIC_URL` (the externally visible URL, used for Host/Origin checks and
OAuth metadata), `HIVE_PROFILE` (`embedded` | `external`), `HIVE_DATABASE_URL` (external profile), `HIVE_TOKENS_FILE`,
`HIVE_ALLOWED_ORIGINS`, `HIVE_RUN_JOBS`, `HIVE_DRAIN_SECONDS`, `HIVE_SHUTDOWN_TIMEOUT`, and for OAuth sign-in
`HIVE_GOOGLE_CLIENT_ID`, `HIVE_GOOGLE_CLIENT_SECRET`, `HIVE_GOOGLE_ALLOWED_DOMAINS`, `HIVE_USERS_FILE`, `HIVE_SECRET_KEY`
(see [OAUTH.md](OAUTH.md)). Everything else (embedders, reranker, quotas, retention, URL ingestion, notifications, rate
limits, `HIVE_ADMIN_UI`) is documented next to its feature and listed in `src/hive_mcp/config.py`.
