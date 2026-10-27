#!/bin/bash
# The one-container promise on a built image: no /data volume (a disposable disk), only a backup volume.
# Writes a note, stops the container, destroys it, starts a brand-new one on the same backups: the note is back.
# Also checks that a second instance cannot start while the first runs.
# Usage: scripts/smoke_single_container.sh [image]   (default hive-mcp:ci)
# SMOKE_ARGS adds docker options, e.g. SMOKE_ARGS='-e HIVE_EMBEDDED_POSTGRES=true' for the Postgres image.
set -euo pipefail
IMAGE=${1:-hive-mcp:ci}
ID=$$
PORT=${PORT:-18098}
BACKUPS=hive-backups-$ID
cleanup() {
  docker rm -fv "hive-a-$ID" "hive-b-$ID" "hive-c-$ID" >/dev/null 2>&1 || true
  docker volume rm "$BACKUPS" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker volume create "$BACKUPS" >/dev/null
# the backup volume is written by the unprivileged container user
docker run --rm -v "$BACKUPS":/backups --user 0 --entrypoint chown "$IMAGE" -R 10001 /backups
start() { # name port [extra docker args...]
  local name=$1 port=$2; shift 2
  docker run -d --name "$name" --network host -v "$BACKUPS":/backups \
    -e HIVE_PORT="$port" -e HIVE_PUBLIC_URL="http://127.0.0.1:$port" -e HIVE_BACKUP_DIR=/backups \
    -e HIVE_BACKUP_LEASE_WAIT=3 -e HIVE_EMBEDDER=hashing ${SMOKE_ARGS:-} "$@" "$IMAGE" >/dev/null
}
ready() { for _ in $(seq 60); do curl -fs "http://127.0.0.1:$1/readyz" >/dev/null && return 0; sleep 1; done; return 1; }
rpc() { # port token method params-json
  curl -fs "http://127.0.0.1:$1/mcp" -H "Authorization: Bearer $2" -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$3\",\"params\":$4}"
}

start "hive-a-$ID" "$PORT"
ready "$PORT" || { docker logs "hive-a-$ID"; echo "first instance did not start" >&2; exit 1; }
TOKEN=$(docker exec "hive-a-$ID" hive token create --label smoke --subject smoke --org acme --project p1 2>/dev/null)
rpc "$PORT" "$TOKEN" tools/call '{"name":"kb_remember","arguments":{"title":"Durable","body":"survives container replacement pelican"}}' | grep -q '"indexed":true'

# a second instance on the same backups must refuse to start while the first holds the lease
if start "hive-c-$ID" "$((PORT + 1))" && docker wait "hive-c-$ID" >/dev/null && \
   [ "$(docker inspect --format '{{.State.ExitCode}}' "hive-c-$ID")" != 0 ]; then
  docker logs "hive-c-$ID" 2>&1 | grep -q "another instance"
else
  echo "a second instance started next to the first one" >&2; exit 1
fi

docker stop -t 30 "hive-a-$ID" >/dev/null
docker rm -v "hive-a-$ID" >/dev/null  # the container and its disk are gone
docker run --rm -v "$BACKUPS":/backups --entrypoint sh "$IMAGE" -c 'ls /backups | grep -q "^hive-auto-" && [ ! -e /backups/.hive-lease ]'

start "hive-b-$ID" "$PORT"
ready "$PORT" || { docker logs "hive-b-$ID"; echo "replacement did not start" >&2; exit 1; }
rpc "$PORT" "$TOKEN" tools/call '{"name":"kb_consult","arguments":{"query":"pelican"}}' | grep -q Durable
docker stop -t 30 "hive-b-$ID" >/dev/null
echo "single-container smoke ok"
