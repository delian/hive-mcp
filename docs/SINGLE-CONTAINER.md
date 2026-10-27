# Everything in one container

Every feature of Hive runs **inside the one server process**: the search indexes, the background jobs, the weekly
digest, group sync, connectors, retention, the usage meter, Slack, OCR, backups. There is no worker, scheduler, queue,
cache or database service to deploy. The embedded profile (SQLite + files) is the default; Postgres + pgvector is an
optional upgrade for bigger or multi-replica installs, never a requirement.

## Try it in one command

```bash
docker build -t hive-mcp .
docker run --rm -p 8080:8080 -v hive:/data hive-mcp hive demo
```
`hive demo` seeds a demo project with example notes, prints a token and the `claude mcp add ...` line, then serves.
(Demo only: it prints a token. Search quality in the demo uses the built-in hashing embedder unless you bake the
real model into the image with `--build-arg BAKE_MODEL=true` and set `HIVE_EMBEDDER=onnx`.)

## Run it for real, locally

```bash
docker run -d --name hive --restart unless-stopped -p 127.0.0.1:8080:8080 -v hive:/data \
  -e HIVE_EMBEDDER=onnx -e HIVE_PUBLIC_URL=https://hive.example.com -e HIVE_SECRET_KEY=$(openssl rand -hex 32) \
  hive-mcp
docker exec hive hive token create --label me --subject me --org acme --project p1
```
State lives in the `/data` volume (the database, stored logs, tokens, users). Back it up with `hive backup`, or let the
server do it (below).

## In the cloud with a disposable disk (Azure Container Apps, Cloud Run, ...)

A cloud container's own disk disappears when the container is replaced, and SQLite must not live on a network share
(its write-ahead log needs real file locking). So: **the data stays on the container's local disk; the server backs
itself up to durable storage and restores from it.**

```bash
HIVE_BACKUP_DIR=/backups              # a mounted Azure Files share / bucket mount / volume
HIVE_BACKUP_EVERY_MINUTES=15          # default; archives are written only when something changed
HIVE_BACKUP_KEEP=8                    # newest archives kept
```
- **Every N minutes**, if anything changed, a backup archive (database snapshot, stored logs, tokens, users, cookie key,
  with checksums) is written to the directory and **read back and verified**; the oldest beyond `HIVE_BACKUP_KEEP` go.
- **A final backup is taken when the server stops** (planned restarts, upgrades, scale-to-zero lose nothing).
- **On start with an empty disk and archives present**, the newest archive that verifies is restored before serving;
  a damaged newest one is skipped for the one before.
- **One writer at a time.** The running instance holds a lease file in the backup directory (refreshed every 10 s,
  deleted on a clean stop). A second instance that finds a live lease waits up to `HIVE_BACKUP_LEASE_WAIT` seconds (90),
  then refuses to start with a clear message instead of creating a diverging copy; the lease of a crashed instance
  is taken over after 45 s. So an overlapping rolling update fails loudly and the old container keeps serving.
- **The price:** a hard crash loses the changes since the last archive (at most the interval). If that is too much, use
  the Postgres profile (`deploy/azure/main.bicep`), whose durability is the database's.

### Azure, one container
`deploy/azure/single.bicep` creates exactly this: one Container App (1 replica, 60 s shutdown grace for the final backup),
one Azure Files share for the archives, a registry and logs. No database.
```bash
az group create -n hive-rg -l westeurope
az deployment group create -g hive-rg -f deploy/azure/single.bicep -p cookieSecret=$(openssl rand -hex 32) deployApp=false
az acr build -r <acrName from outputs> -t hive-mcp:latest --build-arg BAKE_MODEL=true .
az deployment group create -g hive-rg -f deploy/azure/single.bicep -p cookieSecret=<the same value>
```
Extra settings (connectors, Slack, retention, SSO) go in `extraSettings`, e.g.
`-p extraSettings='[{"name":"HIVE_SLACK_TEAM_ID","value":"T0ACME"}]'`; secrets as Container Apps secrets.

**Upgrades.** The platform starts the new revision before stopping the old one. `single.bicep` therefore sets
`HIVE_BACKUP_HANDOFF=true`: the starting revision asks the running one (a *different* revision, identified by
`CONTAINER_APP_REVISION`) to hand over; the old one takes its final backup, releases the lease and exits, and the new one
restores that backup and serves, usually within seconds, with nothing lost. The revision that yielded is written to a
retired list in the backup directory, so when the platform restarts that old container it refuses to start instead of
taking the lease back (it logs "was handed over"). Without hand-off (the default elsewhere) the lease makes an
overlapping start fail loudly and the old instance keeps serving; then upgrade by stopping first:
```bash
az containerapp update -g hive-rg -n hive-mcp --min-replicas 0 --max-replicas 0   # old instance takes its final backup
az deployment group create ... -p imageTag=<new>                                  # starts one replica, restores
```
(`scripts/azure_upgrade_single.sh` does both.) Restoring on a new region or subscription is the same command: create the
share, copy the archives in, deploy. Outside Azure set `HIVE_REVISION` to anything that differs between releases (an
image tag or digest) together with `HIVE_BACKUP_HANDOFF=true`.

## What cannot run in one container
- **Several replicas**: SQLite is single-process (`HIVE_REPLICAS > 1` needs Postgres).
- **Very large knowledge bases**: past a few hundred thousand notes use Postgres, or LanceDB (docs/STORAGE.md).
- **OCR**: needs an image built with `--build-arg WITH_OCR=true` (Tesseract and Poppler are inside the same image).
