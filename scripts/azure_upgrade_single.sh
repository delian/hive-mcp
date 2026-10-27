#!/usr/bin/env bash
# Upgrade the one-container Azure deployment without overlap: stop (final backup), deploy, start (restore).
#   scripts/azure_upgrade_single.sh <resource-group> <image-tag> [extra az deployment args...]
# Needs HIVE_COOKIE_SECRET (the same value as the first deployment) in the environment.
set -euo pipefail
rg="${1:?resource group}"; tag="${2:?image tag}"; shift 2
: "${HIVE_COOKIE_SECRET:?set HIVE_COOKIE_SECRET to the cookie secret used at the first deployment}"
app="${APP_NAME:-hive-mcp}"
echo "stopping $app (it takes a final backup on the way down)..."
az containerapp update -g "$rg" -n "$app" --min-replicas 0 --max-replicas 0 >/dev/null
for _ in $(seq 1 30); do
  count=$(az containerapp replica list -g "$rg" -n "$app" --query 'length(@)' -o tsv 2>/dev/null || echo 0)
  [ "$count" = "0" ] && break
  sleep 5
done
echo "deploying image tag $tag..."
az deployment group create -g "$rg" -f "$(dirname "$0")/../deploy/azure/single.bicep" \
  -p cookieSecret="$HIVE_COOKIE_SECRET" imageTag="$tag" "$@" >/dev/null
echo "done: the new instance restores the newest backup on start"
