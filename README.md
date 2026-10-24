# hive-mcp

A remote MCP server that lets agents and teams share knowledge: session logs, compactions, research,
formulas, measurements and best practices, stored once and found again by any connected agent.

Status: early development. Design and roadmap: [docs/PLAN.md](docs/PLAN.md); storage interfaces: [docs/STORAGE.md](docs/STORAGE.md); services: [docs/SERVICES.md](docs/SERVICES.md); storage benchmark: [docs/BENCHMARK.md](docs/BENCHMARK.md).

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
