# Abuse limits and consent

**Rate limits** (token buckets; per replica by default, shared through Postgres with `HIVE_REPLICAS > 1`; 0 disables a limit):

| Setting | Default | Scope |
|---|---|---|
| `HIVE_RATE_LIMIT_REGISTER` | 30 / hour | `POST /register` (anonymous client registration), per client address |
| `HIVE_RATE_LIMIT_AUTH` | 60 / min | `/authorize`, `/token`, `/revoke`, `/oauth/*`, per client address |
| `HIVE_RATE_LIMIT_MCP` | 1200 / min | `/mcp`, per bearer token (burst = 10 s worth); anonymous calls per address |

Over the limit: HTTP 429 with `Retry-After`. Health and discovery endpoints are never limited.
Behind a tunnel or proxy the client address comes from `X-Forwarded-For` **only** for proxies listed in
`HIVE_FORWARDED_ALLOW_IPS` (default `127.0.0.1`); set it to your proxy's address, or all clients share one bucket.

**Remembered consent:** after a user approves a client once, the approval page is skipped for the same
user + client + redirect URI + scopes for `HIVE_CONSENT_REMEMBER_DAYS` (default 30; 0 = always ask). Denials are
never remembered. The sign-in itself (Google + user directory check) still happens every time, so removing a user
from the directory takes effect immediately.

## Secrets and sessions
`HIVE_SECRET_KEY` (signs login and admin cookies) must be at least 32 characters (`openssl rand -hex 32`); when it is not set a
random key is generated into `<data_dir>/secret.key`, created with mode 0600. Admin sessions are signed cookies and stateless:
signing out removes the cookie from that browser, but a stolen cookie stays valid until it expires (8 h) unless the account's
role is withdrawn, the user is removed/disabled, or the token it was created from is revoked (all checked on every request).
OAuth refresh tokens live 30 days from login and are not extended by use; client registrations also expire after 30 days.

## Untrusted input: what is bounded

| Input | Protection |
|---|---|
| Secrets in text | Redaction before storage; the PEM-key rule is a single linear pass (a lazy regex took 5 s on a 190 KB hostile body and froze the server) and masks a key whose end is missing to the end of the text |
| PDF, Office, HTML files | Parsed in a worker thread (a slow or hostile file cannot stall other users); PDFs over 2,000 pages refused; Office archives capped at 16 MB per part / 64 MB unpacked, DTDs and entities refused; 10 MB via tools, 50 MB via the CLI |
| URL fetching (off by default) | Public addresses only, checked for every redirect and pinned for the connection; IPv4 hidden in IPv4-mapped, NAT64 and 6to4 IPv6 addresses is checked too; 20 MB, per-operation timeout and a 60 s overall deadline |
| Git ingestion | Operator command only; shallow clone, no submodules, no `ext::` transport, symlinks and hidden paths skipped, nothing executed |
| Backups | Archives are extracted with Python's `data` filter after a path check; entries must match a checksummed manifest |

**Prompt injection:** anything stored (including fetched pages and ingested documents) can contain text that tries to
instruct an agent that later reads it. The server cannot neutralise that; it labels external results, keeps provenance
(`source`, author, scope) on every item, and the server `instructions` tell agents to treat retrieved text as data.
Review shared and ingested content like any other document before trusting what agents do with it.

**Supply chain:** CI runs `pip-audit` on the locked dependencies (also weekly), scans the built image with Trivy
(fixable high/critical findings fail the build), and Dependabot proposes updates for Python packages, the base image and
GitHub Actions.
