#!/bin/sh
# Install the small client (hive-hooks) on a machine where a coding agent runs, then set it up.
# Usage: scripts/install_client.sh --url https://kb.example.com/mcp [--editor claude,vscode]
#        [--scope user|project] [--agents-file AGENTS.md] [--save-token]
# All arguments go to `hive-hooks init`. Needs uv (https://docs.astral.sh/uv/) or pipx.
# HIVE_CLIENT_SOURCE overrides where the package comes from (a wheel, a path, a git URL).
set -eu
SOURCE="${HIVE_CLIENT_SOURCE:-hive-hooks @ git+https://github.com/delian/hive-mcp#subdirectory=clients/hooks}"
if command -v uv >/dev/null 2>&1; then
  uv tool install --force "$SOURCE"
elif command -v pipx >/dev/null 2>&1; then
  pipx install --force "$SOURCE"
else
  echo "error: install uv first: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi
exec hive-hooks init "$@"
