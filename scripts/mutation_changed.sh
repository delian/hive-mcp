#!/bin/bash
# Mutation-test only the security-critical modules that a change touched (pull requests and pushes),
# so a surviving mutant shows up in the change that caused it, not a week later.
# Usage: scripts/mutation_changed.sh [base-commit]   (default: the parent of HEAD)
set -uo pipefail
cd "$(dirname "$0")/.."
base="${1:-}"
if [ -z "$base" ] || [ "$base" = "0000000000000000000000000000000000000000" ] || ! git cat-file -e "$base" 2>/dev/null; then
  base="HEAD~1"
fi
patterns=()
while read -r path; do
  # source_paths in pyproject.toml are the modules under mutation testing
  if grep -q "\"$path\"" pyproject.toml; then
    name="${path#src/}"
    patterns+=("$(echo "${name%.py}" | tr / .)*")
  fi
done < <(git diff --name-only "$base" HEAD -- 'src/*.py')
if [ ${#patterns[@]} -eq 0 ]; then
  echo "no security-critical module changed: nothing to mutate"
  exit 0
fi
status=0
for pattern in "${patterns[@]}"; do
  echo "== mutating $pattern"
  rm -rf mutants
  scripts/mutation.sh "$pattern" || status=1
done
exit $status
