#!/bin/bash
# Mutation testing of the security-critical modules (see [tool.mutmut] in pyproject.toml).
# Usage: scripts/mutation.sh [mutant-name-pattern]   e.g. scripts/mutation.sh 'hive_mcp.slack.verify*'
# Exits non-zero when any mutant survives, i.e. a change to the code that no test notices.
set -uo pipefail
cd "$(dirname "$0")/.."
uv run --group mutation mutmut run "$@"
uv run --group mutation mutmut results | tee /tmp/mutmut-results.txt
if grep -q ": survived" /tmp/mutmut-results.txt; then
  echo "surviving mutants: strengthen the tests (uv run --group mutation mutmut show <name>)" >&2
  exit 1
fi
