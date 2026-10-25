# Abuse limits and consent

**Rate limits** (in-process token buckets, per replica; 0 disables a limit):

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
