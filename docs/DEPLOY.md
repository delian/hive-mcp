# Deploying

One image, three ways to run it. Status: the image, the smoke test and the Bicep template were built and
checked in the development sandbox; **nothing has been deployed to a real Azure subscription yet**.

## The image
`docker build --build-arg BAKE_MODEL=true -t hive-mcp .` (about 450 MB). Build args: `BAKE_MODEL` (download and
SHA-256-verify the offline embedding model into the image), `EXTRAS` (optional dependency groups; default
postgres + local-embeddings + documents), `PYTHON_IMAGE` (base image / registry mirror). Behind a TLS-inspecting
proxy add `--secret id=cacert,src=<ca-bundle>`. Runs as a non-root user; state lives in the `/data` volume;
`/healthz` (liveness) and `/readyz` (readiness, fails first during shutdown). `scripts/smoke_docker.sh` checks a
built image end to end: auth, tools, restart with data kept, clean exit on SIGTERM.

## 1. Local, one container
```
cp .env.example .env
docker compose up -d --build        # SQLite + files in the hive-data volume, http://localhost:8080
docker compose exec hive hive token create --label me --subject me --org acme --project p1
```
Add `--profile postgres` for Postgres + pgvector (set `POSTGRES_PASSWORD`; the service is `hive-pg`, stop `hive`
first as both use port 8080).

## 2. Private network + tunnel
Keep `HIVE_BIND=127.0.0.1`; the tunnel container reaches hive over the compose network.
- **Cloudflare:** create a tunnel pointing at `http://hive:8080`, put its token in `TUNNEL_TOKEN`,
  `docker compose --profile cloudflare up -d`. Set `HIVE_PUBLIC_URL` to the tunnel's `https://` address and
  `HIVE_FORWARDED_ALLOW_IPS` to the cloudflared container's address (or `*` on the isolated compose network).
- **Tailscale:** `TS_AUTHKEY` + `--profile tailscale`; edit `deploy/tailscale-serve.json` (set `AllowFunnel` to
  true only if claude.ai/ChatGPT must reach it from the internet; tailnet-only is invisible to them).
Claude.ai, ChatGPT and Gemini connect from the vendor's cloud, so they need a **publicly reachable HTTPS URL**
(Cloudflare tunnel or Tailscale Funnel) and the Google sign-in below. Coding agents on your own machines can use a
private address and static tokens.

## 3. Azure Container Apps
`deploy/azure/main.bicep` creates: Container Apps environment + app (1 replica, probes, 30 s grace), Postgres
Flexible Server 16 with pgvector allow-listed, Azure Files share for `/data`, container registry with a
pull-only managed identity, Log Analytics. Steps:
```
az group create -n hive-rg -l westeurope
export HIVE_DB_PASSWORD=$(openssl rand -hex 16) HIVE_COOKIE_SECRET=$(openssl rand -hex 32)
az deployment group create -g hive-rg -f deploy/azure/main.bicep -p deploy/azure/main.bicepparam deployApp=false
az acr build -r <acrName from outputs> -t hive-mcp:latest --build-arg BAKE_MODEL=true .
az deployment group create -g hive-rg -f deploy/azure/main.bicep -p deploy/azure/main.bicepparam
```
Notes: the Postgres firewall allows Azure services only (move to VNet integration + private endpoint to take it
off the public network); the app is one replica because token/user files and rate limits are per instance;
upgrades are a new revision (old one drains: readiness fails first, in-flight requests finish, clients reconnect
without restarting). The 100 GB scale target needs a larger `dbSku`/`dbStorageGb`; blobs (session logs) live on
the Azure Files share.

## Google Workspace sign-in
Create an OAuth client (type: Web application) with redirect URI `<HIVE_PUBLIC_URL>/oauth/google/callback`, set
`HIVE_GOOGLE_CLIENT_ID/SECRET` and `HIVE_GOOGLE_ALLOWED_DOMAINS=["yourdomain.com"]`, then add people:
`hive user add a@yourdomain.com --org acme --role member --project p1`. In claude.ai add a custom
connector with the URL `<HIVE_PUBLIC_URL>/mcp`.
