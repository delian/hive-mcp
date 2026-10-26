# hive-mcp

A remote MCP server that lets agents and teams share knowledge: session logs, compactions, research,
formulas, measurements and best practices, stored once and found again by any connected agent.

Private by default: what an agent learns stays with its author until someone shares it; wider sharing needs an
approver. Sign in with Google Workspace (claude.ai, ChatGPT, Gemini) or bearer tokens (pipelines, CLI). SQLite
(one container, one volume) or Postgres + pgvector; hybrid keyword + vector search with optional reranking.

Status: feature complete for a first deployment, tested against fakes of Google and of the clients. **Not yet verified
against real Google, real assistants or a real Azure deployment** - see the open items in [docs/PLAN.md](docs/PLAN.md).

## Documentation

| Topic | Page |
|---|---|
| Agent setup (Claude Code hooks, AGENTS.md) | [AGENTS](docs/AGENTS.md) |
| Tools, auth, configuration | [MCP](docs/MCP.md), [OAuth / Google sign-in](docs/OAUTH.md) |
| Deploy (Docker, Azure, Tailscale), backup and restore | [DEPLOY](docs/DEPLOY.md), [BACKUP](docs/BACKUP.md), [HARDENING](docs/HARDENING.md) |
| Search quality, embedders, reranker | [EVALUATION](docs/EVALUATION.md), [EMBEDDINGS](docs/EMBEDDINGS.md) |
| Sharing and approvals | [PROMOTION](docs/PROMOTION.md) |
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

Docker (one container, data in the `/data` volume):

```bash
docker build -t hive-mcp .
docker run -p 8080:8080 -v hive:/data hive-mcp
docker exec <container> hive token create --label me --subject me --org myorg --project demo
```

See [docs/MCP.md](docs/MCP.md) for the tools, authentication and configuration, and [docs/OAUTH.md](docs/OAUTH.md) for
connecting claude.ai, ChatGPT and Gemini through Google sign-in.

## Development

```bash
uv run ruff format . && uv run ruff check .
uv run mypy src
uv run python scripts/check_file_size.py src   # source files: target 8 KB, hard limit 16 KB
uv run pytest
```
