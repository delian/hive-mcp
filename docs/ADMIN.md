# Administration

**Who is an admin:** role `admin` (`hive user add ... --role admin`, or a token created with `--role admin`).

| Tool | What |
|---|---|
| `kb_admin_usage` | items and characters stored per person (archived excluded), biggest first |
| `kb_admin_audit(actor, action, target, after_seq, limit)` | the audit trail, 500 per page; oldest first, or `newest_first`; page with the last `seq` as `after_seq` (or `before_seq` when newest first) |

Audit actions include `item.create/update/relocate/purge`, `promotion.request/approved/rejected/withdrawn/stale`,
`edge.add/remove`, `user.forget`. Events hold ids and short facts (kind, scope), never content.

**Quotas** (off by default, admins exempt): `HIVE_QUOTA_ITEMS_PER_USER`, `HIVE_QUOTA_CHARS_PER_USER`. They apply to
new items (including reports, ingested parts and merges), not to edits. The usage query is cached for 30 seconds
per person per process, so with several replicas a person can overshoot slightly. A refused write says what to
do (archive or purge, or ask an admin). Archiving frees quota.

## Web admin page (opt-in)

`HIVE_ADMIN_UI=true` serves a small page at `/admin` for people who prefer a browser to tools. It has no
JavaScript and uses the same data and rules as the tools; nothing here is a second code path for permissions.

| Page | Who | What |
|---|---|---|
| `/admin/promotions` | approvers and admins | pending requests with the item text; **Approve** / **Reject** with an optional note |
| `/admin/promotions/history` | approvers (their scopes) and admins (all) | every request, newest first: status, who decided, when, note; filter by status, 50 per page, most recent 1,000 considered |
| `/admin/audit` | admins | audit trail, newest first, filters for who / what / target, 100 per page |
| `/admin/usage` | admins | items and characters per person, plus the quota limits in force |
| `/admin/users` | admins | add or change a person (role, teams, projects, approver scopes), disable / enable, remove, **sign out everywhere**, forget a pinned Google account |
| `/admin/tokens` | admins | list tokens of the organisation, create one (shown **once**, never listed again), revoke |

**Guards on people and tokens:** you cannot remove, disable or demote yourself; the last enabled admin of the organisation
cannot be removed, disabled or demoted; people and tokens of other organisations are invisible and untouchable; a new token
belongs to your organisation. Disabling or removing someone ends their sessions at once. Every change is in the audit trail
(`admin.user.save/disable/enable/remove/signout`, `admin.token.create/revoke`, `admin.login`); token secrets are never logged.

**Signing in:** with Google (the same Workspace accounts and `hive user` directory as the assistants; add
`<HIVE_PUBLIC_URL>/admin/google/callback` to the OAuth client's authorised redirect URIs), or by pasting a static
token (`hive token create`). Either way the role comes from the directory or the token, and it is looked up again on
every request: disabling a user or revoking/expiring a token ends their session immediately. A session lasts 8 hours.

**Protections:** session cookie is `HttpOnly`, `SameSite=Lax` (strict would be dropped on Google's redirect back),
`Secure` on https, path `/admin`, signed with `HIVE_SECRET_KEY`. Every POST must carry the session's CSRF token *and*
a matching `Origin` header. Sessions are kept on the server, so signing out (or an admin's *sign out everywhere*) ends them for real. Pages send `no-store`, a CSP without scripts, `frame-ancestors 'none'` and cannot be framed.
Sign-in attempts share the login rate limit. Each sign-in is written to the audit log (`admin.login`).

The page is as exposed as the MCP endpoint; with Tailscale Serve or a private network, keep it off the public funnel.

## Activity and per-token limits

**Activity** (`/admin/activity`, tool `kb_admin_activity(days=30)`; admins only): per person the tool calls, writes,
stored items and last seen, plus per-team totals and the busiest tools. Tool calls are counted in memory and saved
every 5 minutes (and at shutdown) as one `usage.tools` audit event per person, with counts only; so the report covers
every replica and survives restarts, and the newest few minutes are missing. Teams come from the user directory.
Prometheus metrics stay per tool, never per person (`docs/OBSERVABILITY.md`). Quotas on stored items/characters are
unchanged (`HIVE_QUOTA_*`).

**A token's own request limit.** `hive token create ... --rate-per-min 600` gives one token a limit different from the
server default (`HIVE_RATE_LIMIT_MCP`, per token): lower for a noisy pipeline, higher for a trusted bulk importer.
`hive token list` shows it. The limit is enforced per token on `/mcp` (shared across replicas with Postgres).
