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
random key is generated into `<data_dir>/secret.key`, created with mode 0600. Admin sessions are kept on
the server (only a signed random id is in the cookie, and only its hash in the store): signing out destroys the session, so a
copied cookie stops working at once; "sign out everywhere" ends all of a person's sessions; sessions also end after 8 h, when
the person is removed or disabled, or when the token they signed in with is revoked (checked on every request).
OAuth refresh tokens live 30 days from login and are not extended by use; client registrations also expire after 30 days.

## Untrusted input: what is bounded

| Input | Protection |
|---|---|
| Secrets in text | Redaction before storage; the PEM-key rule is a single linear pass (a lazy regex took 5 s on a 190 KB hostile body and froze the server) and masks a key whose end is missing to the end of the text |
| PDF, Office, HTML files | Parsed in a worker thread (a slow or hostile file cannot stall other users); PDFs over 2,000 pages refused; Office archives capped at 16 MB per part / 64 MB unpacked, DTDs and entities refused; 10 MB via tools, 50 MB via the CLI |
| Scans and images (OCR, off by default) | Separate Tesseract/Poppler processes with a 60 s time limit per page, 300 s per document, 2 GB memory limit and a page cap; files travel by stdin or a private temp directory, never a command line |
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

## Text that attacks the agents reading it

Shared memory is read by other people's agents, so a note is also an attack surface. Every write
(`kb_remember`, `kb_update`, ingest, sessions) goes through `security/sanitize.py`:

- **Hidden characters** (Unicode tag characters, bidi overrides, zero-width) are removed; they hide
  text from the humans who review promotions and have no legitimate use in a note.
- **Secrets** are masked (`security/redact.py`), as before.
- **Prompt-injection heuristics** (`security/injection.py`: "ignore previous instructions", fake role
  markers, "send the API keys to ...", image-URL exfiltration, "don't tell the user") add the tag
  `flag/injection` and are reported in the write result as `injection:<heuristic>`. The tag follows
  the content: editing the text clean removes it, editing it back restores it. Approvers see the tag
  on the item they are asked to promote. Matches are flags, not rejections: heuristics can be wrong,
  and a hard block would be easy to probe. Agents should treat flagged items as untrusted data.

## Second review pass (chat front doors, connectors, integrations, model, leases)

Reviewed by reading each new module against the same checklist (who can reach it, what is trusted, what leaves the
server, what an attacker controls) and by fuzzing the parsers (`tests/fuzz`, Hypothesis plus adversarial inputs). Found
and fixed:

- **A ReDoS in the prompt-injection heuristic** (found by the fuzz test): `^\s*` at every line start is quadratic on a
  long run of newlines (a 100 KB note took minutes). The pattern now uses `[ \t]`; adversarial inputs for every text check
  are a regression test with a time limit.
- **Teams service URL allow-list** accepted any `*.trafficmanager.net` host, a namespace anyone can register names in. Only
  `smba.trafficmanager.net` and `*.botframework.com/.us` are allowed (replies carry the bot token).
- **Undecryptable integration secrets** (after `HIVE_SECRET_KEY` changed) made every request to the chat routes fail with
  an error. The integration is now switched off with a log line until an admin enters the secrets again.
- `/teams/*` was missing from the login-route rate limit (Slack already was).
- A corrupt or truncated backup archive raised a raw tar error instead of a validation error (found earlier).
- `kb_admin_integration_set` silently ignored secret arguments; it now refuses them with an explanation.

Checked and fine: Slack and Teams requests are authenticated before the body is trusted (signature or Bot Framework token,
replay protection); chat replies escape markup (Slack mrkdwn) or are plain text (Teams); channel replies quote only what the
whole channel may see; identities come from the user directory and are checked on every call; integration secrets are
encrypted at rest, never returned, and not accepted through chat tools; connectors fetch with the URL safety rules or fixed
API hosts, put everything through the write sanitiser, and never retire documents after a failed or suspicious listing;
the model only sees notes at or above `HIVE_LLM_MIN_SCOPE` and its answers are only ever proposals; lease and retired-list
files contain no user input in their paths. Office and PDF extractors survive random bytes, malformed containers, a zip
bomb and 100,000-level nesting within the time limits (tests/fuzz).

Not covered: a penetration test against a real deployment, dependency-level fuzzing of the PDF/Office libraries beyond
what the tests above exercise, and anything that needs real Slack, Teams, Google or Azure to observe.

## Mutation testing

A passing test suite only says the tests agree with the code. `scripts/mutation.sh` (mutmut) changes
the code in small ways (flips a comparison, drops a check, alters a constant) and runs the fast tests
against each change; a change that no test notices is a *surviving mutant* and fails the script.
It covers the code where a silent change would matter most:
- sign-in and tokens: static bearer tokens, the OAuth token service (issue, rotation, reuse detection, family
  revocation), the login cookie and its key, token revocation;
- chat front doors: Slack signature checks, Teams token verification, the Teams connector's service-URL guard and
  token cache, the Teams message extension;
- other: the prompt-injection heuristics, retention rules, the backup lease and hand-off, the ranking nudge,
  connector settings and the connector sync loop (what is retired, when it refuses to).
All 1,712 mutants are killed at the time of writing. CI runs it weekly (and on demand through *Run workflow*), not on
every push, because it takes a few minutes.

Some mutants cannot be killed because they change nothing observable: a header name's case, the default size of
`secrets.token_urlsafe`, a log message's text, a sleep length, an HTTP client timeout. Those lines carry
`# pragma: no mutate` with the reason (mutmut reads it on the first line of a statement, so such values are kept in
a one-line constant). That is deliberate and should stay rare. Mutants in *messages* that tell a person what to do
are killed with exact-text assertions instead. To look at a survivor: `uv run --group mutation mutmut show <name>`
; to widen the scope, add files to `source_paths` under `[tool.mutmut]` in `pyproject.toml`
together with the tests that cover them (`pytest_add_cli_args_test_selection`). A mutant reported as "no tests" is not
covered by the selected tests at all: add the tests that exercise it.
