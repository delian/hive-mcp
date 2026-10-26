# Administration

**Who is an admin:** role `admin` (`hive user add ... --role admin`, or a token created with `--role admin`).

| Tool | What |
|---|---|
| `kb_admin_usage` | items and characters stored per person (archived excluded), biggest first |
| `kb_admin_audit(actor, action, target, after_seq, limit)` | the audit trail, oldest first, 500 per page; page with the last `seq` |

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
| `/admin/audit` | admins | audit trail, filters for who / what / target, oldest first, 100 per page |
| `/admin/usage` | admins | items and characters per person, plus the quota limits in force |

**Signing in:** with Google (the same Workspace accounts and `hive user` directory as the assistants; add
`<HIVE_PUBLIC_URL>/admin/google/callback` to the OAuth client's authorised redirect URIs), or by pasting a static
token (`hive token create`). Either way the role comes from the directory or the token, and it is looked up again on
every request: disabling a user or revoking/expiring a token ends their session immediately. A session lasts 8 hours.

**Protections:** session cookie is `HttpOnly`, `SameSite=Lax` (strict would be dropped on Google's redirect back),
`Secure` on https, path `/admin`, signed with `HIVE_SECRET_KEY`. Every POST must carry the session's CSRF token *and*
a matching `Origin` header. Pages send `no-store`, a CSP without scripts, `frame-ancestors 'none'` and cannot be framed.
Sign-in attempts share the login rate limit. Each sign-in is written to the audit log (`admin.login`).

The page is as exposed as the MCP endpoint; with Tailscale Serve or a private network, keep it off the public funnel.
