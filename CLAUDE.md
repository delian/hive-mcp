# Working on hive-mcp

- **Keep the requirements current.** `docs/REQUIREMENTS.md` (what the system must do, with the decisions and
  the ledger of what the user asked) and `docs/REQUIREMENTS-ANNEX.md` (the exact tools, routes, settings,
  CLI, schemas and constants) must describe the code well enough to rebuild it. Whenever you change `src/`,
  update them in the same commit: add or change the `REQ-` entry, keep the Annex catalogue exact, and when
  the user's instruction decides something, add it to the ledger (latest wins on a conflict).
  If a change really affects no requirement, put a line `Requirements: none` in the commit message.
  CI runs `scripts/check_requirements.py`, and `tests/unit/test_docs_drift.py` checks the Annex lists every
  tool, setting and command.
- Before pushing: `uv run ruff format . && uv run ruff check . && uv run mypy src clients/hooks/src`,
  `uv run python scripts/check_file_size.py src clients/hooks/src`, and `uv run pytest`
  (`scripts/start_test_postgres.sh` first for the Postgres variants).
