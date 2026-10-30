# hive-mcp

**Shared memory for a team and its AI agents.** A remote [MCP](https://modelcontextprotocol.io) server where agents and
people save what they learn (how-tos, decisions, measurements, research, session reports) and find it again, with
**private-by-default** knowledge, **approval-gated sharing**, and search that understands meaning.

* Agents (Claude Code, Cursor, VS Code, claude.ai, ChatGPT, Gemini) consult and save through MCP tools; hooks make it automatic.
* People use the same knowledge from **Slack**, **Microsoft Teams**, a small **admin web UI**, email digests and a CLI.
* Everything runs **in one container**: search indexes, background jobs, scheduled imports, backups. No queue, worker or
  database service is required (SQLite by default; PostgreSQL + pgvector when you outgrow it, even inside the same container).

> Status: feature complete for a first deployment. Slack, Teams, Google/Entra sign-in, the cloud connectors and an Azure
> deployment are verified against fakes and the platforms' documented formats, **not yet against the real services**.
> See [docs/PLAN.md](docs/PLAN.md) and the "Known limits" in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## What you get

| | |
|---|---|
| **Knowledge model** | notes of several kinds (howto, decision, formula, measurement, research, session…), tags, revisions with full history, links (supersedes, contradicts…), entities |
| **Privacy and sharing** | private → project → team → organisation; self-service up to a project, **approvers** for wider sharing; org isolation; EXTERNAL labels for knowledge from other groups |
| **Search** | keyword + semantic (hybrid, rank fusion), freshness and scope weighting, relevance floor, optional reranker, search feedback and evaluation tools |
| **Getting knowledge in** | agent hooks and session reports; files (PDF, Office, HTML, code, images with OCR); URLs; git; **connectors** for Drive, SharePoint, GitHub, Confluence, Jira, Notion; Slack and Teams capture |
| **Keeping it healthy** | duplicate and conflict detection, stale-note review, merge proposals (optional language model), retention rules (archive/purge), full erasure of a person, weekly digest |
| **Sign-in** | static tokens; OAuth for claude.ai/ChatGPT/Gemini via Google, Microsoft Entra ID or any OIDC provider; user directory; group sync |
| **Operations** | self-backup with verification, restore on an empty disk, one-writer lease and zero-downtime hand-off, SQLite⇄PostgreSQL migration with a proof, metrics, JSON logs, rate limits, quotas, audit trail |
| **Assurance** | one conformance suite for every storage backend, fuzzing, mutation testing of the security-critical code, stress and regression tests |

## Quick start

```bash
# a populated demo server (example notes, a token and the `claude mcp add` line are printed)
docker build -t hive-mcp .
docker run --rm -p 8080:8080 -v hive:/data hive-mcp hive demo
```

```bash
# from source
uv sync
export HIVE_DATA_DIR=./data HIVE_PUBLIC_URL=http://localhost:8080
uv run hive token create --label me --subject me --org myorg --project demo   # prints your bearer token (once)
uv run hive serve                                                              # MCP at http://localhost:8080/mcp

# connect an agent (hooks + editor config + AGENTS.md note; the token is never written to a project file)
export HIVE_TOKEN=<the token>
uv run hive-hooks init --url http://localhost:8080/mcp --editor claude,vscode,cursor --agents-file AGENTS.md
```

In the cloud with a disposable disk, set `HIVE_BACKUP_DIR` to durable storage: the server backs itself up there and
restores from it ([deploy/azure/single.bicep](deploy/azure/single.bicep) is a ready one-container Azure template).

## Documentation

| Read this | For |
|---|---|
| **[docs/USAGE.md](docs/USAGE.md)** | installing, connecting agents and people, day-to-day operation, **use cases** |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | how it is built: process, layers, data model, flows, security, deployment, quality |
| **[docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)** | the requirements accumulated over development (what was asked, decided, assumed), with acceptance criteria and traceability; written so the system could be regenerated from it |
| **[docs/REQUIREMENTS-ANNEX.md](docs/REQUIREMENTS-ANNEX.md)** | exact catalogue: tools, routes, CLI, settings, schemas, constants, formats |
| Agent setup | [AGENTS](docs/AGENTS.md), [MCP](docs/MCP.md), [OAUTH](docs/OAUTH.md) |
| Deploy and operate | [DEPLOY](docs/DEPLOY.md), [SINGLE-CONTAINER](docs/SINGLE-CONTAINER.md), [BACKUP](docs/BACKUP.md), [SIZING](docs/SIZING.md), [MIGRATE](docs/MIGRATE.md), [HARDENING](docs/HARDENING.md), [OBSERVABILITY](docs/OBSERVABILITY.md), [ADMIN](docs/ADMIN.md), [GROUPS](docs/GROUPS.md) |
| Chat and imports | [SLACK](docs/SLACK.md), [TEAMS](docs/TEAMS.md), [CONNECTORS](docs/CONNECTORS.md), [INGEST](docs/INGEST.md) |
| Knowledge work | [PROMOTION](docs/PROMOTION.md), [CURATION](docs/CURATION.md), [DIGEST](docs/DIGEST.md), [RETENTION](docs/RETENTION.md), [GRAPH](docs/GRAPH.md), [ENTITIES](docs/ENTITIES.md), [HISTORY](docs/HISTORY.md), [LLM](docs/LLM.md) |
| Search quality | [EVALUATION](docs/EVALUATION.md), [EMBEDDINGS](docs/EMBEDDINGS.md), [BENCHMARK](docs/BENCHMARK.md) |
| Design notes | [PLAN](docs/PLAN.md), [STORAGE](docs/STORAGE.md), [SERVICES](docs/SERVICES.md) |

## Repository layout

```
src/hive_mcp/    the server (domain, ports, adapters, services, mcp_api, webui, slack, teams, ops, ...)
clients/hooks/   hive-hooks: the slim client package (Claude Code hooks, editor config, init)
tests/           unit, services, conformance (all backends), integration, security, fuzz, regression, stress
evals/           retrieval, conflict and merge evaluation sets
deploy/          Azure Bicep templates, compose file
scripts/         smoke tests, mutation testing, install script, file-size guard
docs/            documentation (see above)
```

## Development

```bash
uv sync
uv run ruff format . && uv run ruff check .
uv run mypy src clients/hooks/src
uv run python scripts/check_file_size.py src clients/hooks/src   # target 8 KB, hard limit 16 KB per file
uv run pytest                                    # all tests (Postgres variants run when HIVE_TEST_PG_DSN is set)
uv run pytest -m stress                          # only the stress tests; HIVE_STRESS_SCALE=5 makes them bigger
uv run pytest tests/regression                   # one test per bug found in review (BUG-<area>-<n>)
scripts/mutation.sh                              # mutation testing of the security-critical modules (about 25 minutes)
scripts/mutation_changed.sh origin/main          # only the modules your change touched
```

A Postgres server with pgvector for the tests: `scripts/start_test_postgres.sh`, then
`export HIVE_TEST_PG_DSN="host=/tmp port=55432 dbname=hive_test user=postgres"`.
