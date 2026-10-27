# hive-mcp

A remote MCP server that lets agents and teams share knowledge: session logs, compactions, research,
formulas, measurements and best practices, stored once and found again by any connected agent.

Private by default: what an agent learns stays with its author until someone shares it; wider sharing needs an
approver. Sign in with Google Workspace (claude.ai, ChatGPT, Gemini) or bearer tokens (pipelines, CLI). SQLite
(one container, one volume) or Postgres + pgvector; hybrid keyword + vector search with optional reranking.

Everything runs **inside one container**: indexes, jobs, digest, connectors, Slack, retention, backups. No database,
queue or worker service is needed ([SINGLE-CONTAINER](docs/SINGLE-CONTAINER.md)).

Status: feature complete for a first deployment, tested against fakes of Google and of the clients. **Not yet verified
against real Google, real assistants or a real Azure deployment** - see the open items in [docs/PLAN.md](docs/PLAN.md).

## Documentation

| Topic | Page |
|---|---|
| Agent setup (Claude Code hooks, AGENTS.md) | [AGENTS](docs/AGENTS.md) |
| Tools, auth, configuration | [MCP](docs/MCP.md), [OAuth / Google sign-in](docs/OAUTH.md) |
| Deploy (Docker, Azure, Tailscale), backup and restore, SQLite/Postgres migration | [DEPLOY](docs/DEPLOY.md), [BACKUP](docs/BACKUP.md), [MIGRATE](docs/MIGRATE.md), [HARDENING](docs/HARDENING.md) |
| Search quality, embedders, reranker | [EVALUATION](docs/EVALUATION.md), [EMBEDDINGS](docs/EMBEDDINGS.md) |
| Sharing and approvals | [PROMOTION](docs/PROMOTION.md) |
| One container (local, Azure, any cloud) | [SINGLE-CONTAINER](docs/SINGLE-CONTAINER.md) (self-backup, restore, one-command demo) |
| Catching up | [DIGEST](docs/DIGEST.md) (kb_digest, project digests, weekly email, stale-note review) |
| Bringing knowledge in | [CONNECTORS](docs/CONNECTORS.md) (Drive, SharePoint, GitHub, Confluence, Jira, Notion, URLs), [SLACK](docs/SLACK.md), [TEAMS](docs/TEAMS.md), [INGEST](docs/INGEST.md) |
| Optional model | [LLM](docs/LLM.md) (merge proposals, contradiction checks, ranking nudges) |
| Knowledge work | [CURATION](docs/CURATION.md), [GRAPH](docs/GRAPH.md), [ENTITIES](docs/ENTITIES.md), [HISTORY](docs/HISTORY.md), [INGEST](docs/INGEST.md) |
| Operating | [ADMIN](docs/ADMIN.md) (audit, usage, quotas, web page), [GROUPS](docs/GROUPS.md) (Workspace group sync), [RETENTION](docs/RETENTION.md), [OBSERVABILITY](docs/OBSERVABILITY.md) (logs, metrics, alerts) |
| Design | [PLAN](docs/PLAN.md), [STORAGE](docs/STORAGE.md), [SERVICES](docs/SERVICES.md), [BENCHMARK](docs/BENCHMARK.md) |

## Quick start

```bash
uv sync
export HIVE_DATA_DIR=./data HIVE_PUBLIC_URL=http://localhost:8080
uv run hive token create --label me --subject me --org myorg --project demo   # prints your bearer token
uv run hive serve                                                              # MCP at http://localhost:8080/mcp
```

Docker, one command to a populated server (example notes, a token and the `claude mcp add` line are printed):

```bash
docker build -t hive-mcp .
docker run --rm -p 8080:8080 -v hive:/data hive-mcp hive demo
```

Docker for real use (one container, data in the `/data` volume):

```bash
docker run -d -p 8080:8080 -v hive:/data hive-mcp
docker exec <container> hive token create --label me --subject me --org myorg --project demo
```

In the cloud with a disposable disk, set `HIVE_BACKUP_DIR` to durable storage: the server backs itself up there and
restores from it (`deploy/azure/single.bicep` is a ready one-container Azure template).

See [docs/MCP.md](docs/MCP.md) for the tools, authentication and configuration, and [docs/OAUTH.md](docs/OAUTH.md) for
connecting claude.ai, ChatGPT and Gemini through Google sign-in.

## Development

```bash
uv run ruff format . && uv run ruff check .
uv run mypy src
uv run python scripts/check_file_size.py src   # source files: target 8 KB, hard limit 16 KB
uv run pytest
```
