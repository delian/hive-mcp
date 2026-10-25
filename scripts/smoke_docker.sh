#!/bin/bash
# Smoke-test a built image: starts it, mints a token, calls the MCP endpoint, restarts, checks the data survived.
# Usage: scripts/smoke_docker.sh [image]   (default hive-mcp:ci)
set -euo pipefail
IMAGE=${1:-hive-mcp:ci}
NAME=hive-smoke-$$
PORT=${PORT:-18099}
VOL=$NAME-data
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; docker volume rm "$VOL" >/dev/null 2>&1 || true; }
trap cleanup EXIT
run() {
  docker run -d --name "$NAME" --network host -v "$VOL":/data -e HIVE_PORT="$PORT" \
    -e HIVE_PUBLIC_URL="http://127.0.0.1:$PORT" -e HIVE_EMBEDDER="${HIVE_EMBEDDER:-hashing}" "$IMAGE" >/dev/null
  for _ in $(seq 60); do curl -fs "http://127.0.0.1:$PORT/readyz" >/dev/null && return 0; sleep 1; done
  docker logs "$NAME"; echo "server did not become ready" >&2; exit 1
}
rpc() { # method params-json
  curl -fs "http://127.0.0.1:$PORT/mcp" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$1\",\"params\":$2}"
}
run
TOKEN=$(docker exec "$NAME" hive token create --label smoke --subject smoke --org acme --project p1 2>/dev/null)
[ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/mcp" -d '{}' -H 'Content-Type: application/json')" = 401 ]
rpc tools/list '{}' | grep -q kb_consult
rpc tools/call '{"name":"kb_remember","arguments":{"title":"Smoke","body":"smoke test pelican"}}' | grep -q '"indexed":true'
docker restart "$NAME" >/dev/null
for _ in $(seq 60); do curl -fs "http://127.0.0.1:$PORT/readyz" >/dev/null && break; sleep 1; done
rpc tools/call '{"name":"kb_consult","arguments":{"query":"pelican"}}' | grep -q Smoke
docker stop -t 20 "$NAME" >/dev/null
[ "$(docker inspect --format '{{.State.ExitCode}}' "$NAME")" = 0 ]
echo "smoke ok"
