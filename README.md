# hive-mcp

A remote MCP server that lets agents and teams share knowledge: session logs, compactions, research,
formulas, measurements and best practices, stored once and found again by any connected agent.

Status: early development (Phase 0 skeleton). The full design and roadmap are in [docs/PLAN.md](docs/PLAN.md).

## Quick start

```bash
uv sync
uv run hive serve            # MCP endpoint on http://localhost:8080/mcp, health on /healthz
```

Docker (one container, data in the `/data` volume):

```bash
docker build -t hive-mcp .
docker run -p 8080:8080 -v hive:/data hive-mcp
```

Configuration is through `HIVE_*` environment variables (see `src/hive_mcp/config.py`).

## Development

```bash
uv run ruff format . && uv run ruff check .
uv run mypy src
uv run python scripts/check_file_size.py src   # source files: target 8 KB, hard limit 16 KB
uv run pytest
```
