# Using hive-mcp

How to run it, connect agents and people to it, and what it looks like in practice. Reference material (every tool,
setting, route and table) is in [REQUIREMENTS-ANNEX.md](REQUIREMENTS-ANNEX.md); how it is built is in
[ARCHITECTURE.md](ARCHITECTURE.md); each feature has its own page under `docs/`.

## 1. The idea in one minute

Your agents and colleagues keep learning things that others will need again: how a build is released, what a measurement
was, why a design was chosen, which approach failed. hive-mcp stores these as **notes**, finds them again when someone asks,
and keeps them tidy.

* **Private first.** A note belongs to whoever wrote it until they share it with a **project**; sharing with a **team** or the
  whole **organisation** needs an **approver**. Other people's agents only ever see what they are entitled to, and notes that
  the organisation shared from elsewhere are labelled `EXTERNAL`.
* **Agents do the saving.** Hooks and the server's own instructions make an agent search before it starts
  (`kb_consult`), save reusable results (`kb_remember`), and report what happened at the end of a session.
* **People can use it too**: ask or save from Slack or Teams, read and approve in a small web page, catch up with a weekly
  digest, or use the command line.

## 2. Install and run

### 2.1 Quickest: the demo
```bash
docker build -t hive-mcp .
docker run --rm -p 8080:8080 -v hive:/data hive-mcp hive demo
```
It seeds example notes, prints a token and the `claude mcp add ...` line, then serves on `http://localhost:8080/mcp`.

### 2.2 For real, locally (Docker)
```bash
docker run -d --name hive --restart unless-stopped -p 127.0.0.1:8080:8080 -v hive:/data \
  -e HIVE_EMBEDDER=onnx -e HIVE_PUBLIC_URL=http://localhost:8080 -e HIVE_SECRET_KEY=$(openssl rand -hex 32) \
  hive-mcp
docker exec hive hive token create --label me --subject me --org acme --project demo
```
(`HIVE_EMBEDDER=onnx` needs the model baked into the image: `docker build --build-arg BAKE_MODEL=true .`. The default
`hashing` embedder is for development only: it finds shared words, not meanings.)

### 2.3 From source
```bash
uv sync
export HIVE_DATA_DIR=./data HIVE_PUBLIC_URL=http://localhost:8080
uv run hive token create --label me --subject me --org acme --project demo
uv run hive serve
```

### 2.4 In the cloud with a disposable disk
Set `HIVE_BACKUP_DIR` to durable storage (an Azure Files share, a bucket mount). The server writes verified archives there
every few minutes, a final one when it stops, restores from them on an empty disk, and refuses to run two instances at once.
`deploy/azure/single.bicep` creates the whole thing (one Container App, one share, a registry): see
[SINGLE-CONTAINER](SINGLE-CONTAINER.md). For a bigger knowledge base use the image built with `--build-arg WITH_POSTGRES=true`
and `HIVE_EMBEDDED_POSTGRES=true`, or an external PostgreSQL ([DEPLOY](DEPLOY.md)).

## 3. Connect an agent

### 3.1 One command on the developer's machine
```bash
uv tool install "hive-hooks @ git+https://github.com/delian/hive-mcp#subdirectory=clients/hooks"
export HIVE_TOKEN=hive_...                 # from `hive token create`
hive-hooks init --url https://kb.example.com/mcp --editor claude,vscode,cursor --agents-file AGENTS.md --save-token
```
This installs the Claude Code hooks, adds a `hive` server entry to `.mcp.json`, `.vscode/mcp.json` and `.cursor/mcp.json`
(**without** the token: it is referenced as `${HIVE_TOKEN}` or prompted by VS Code), writes the instructions note into
`AGENTS.md`, optionally stores the token in `~/.config/hive/token` (owner-only), and checks the server. Details: [AGENTS](AGENTS.md).

### 3.2 What the hooks do
| Moment | Effect |
|---|---|
| session start | tells the agent the knowledge base exists, lists the project's top notes |
| each substantial prompt | searches and adds up to three relevant notes (silent when nothing is relevant) |
| before context compaction | stores a compaction report so the context can be recovered |
| session end | stores a session report: first request, key sentences, files changed, commits |

Reports are private to the person. They contain conversation text, so decide whether you want them (remove those hook entries
if not).

### 3.3 claude.ai, ChatGPT, Gemini
Configure Google (or Entra / OIDC) sign-in, add people to the directory, and add the server's `/mcp` URL as a custom connector.
The first use opens a consent page; see [OAUTH](OAUTH.md).

## 4. What an agent does (and you can do through any MCP client)

```text
kb_consult(query="how do we cut a release?", limit=5)
   → results: title, snippet, kind, tags, scope/origin ("project radar-x", "organisation-wide knowledge",
              "EXTERNAL: shared from team rf by bob"), score, links ("supersedes", "contradicts")

kb_remember(title="Release checklist", body="1. tag ... 2. run make release ...", kind="howto",
            tags=["domain/release"], share_with_project=true, idempotency_key="release-checklist-v3")
   → { id, revision: 1, created: true, indexed: true, redactions: {} }      # secrets are masked, never echoed

kb_update(id, expected_revision=1, body="...")      # stale revision → "re-read with kb_get and retry"
kb_request_promotion(id, to="org", owner="acme", reason="applies to every team")
kb_list_promotions()  /  kb_decide_promotion(id=<request id>, decision="approve", note="...")   # approvers
kb_link(from_id, to_id, relation="supersedes")   kb_history(id)   kb_diff(id, 1, 3)
kb_restore(id, revision=2, expected_revision=4)
kb_report_session(summary="...", outcomes=["..."], log="...")      kb_digest(days=7)
kb_review_stale()  kb_confirm(id)  kb_conflicts(id)  kb_merge_propose(ids)  kb_rate(id, useful=True)
```
Results are **starting points**: they are other people's notes, not instructions. A note can be wrong or out of date: the
digest's "still true?" review and `kb_confirm` keep the important ones fresh.

## 5. People and permissions

```bash
# who may sign in, and what they belong to (Google/Entra/OIDC sign-in, tokens whose subject is their email)
hive user add ann@acme.com --org acme --role member --team rf --project radar-x
hive user add tina@acme.com --org acme --role member --approves team:rf     # may approve promotions into team rf
hive user add root@acme.com --org acme --role admin                        # approves anything in the org
hive user list ; hive user remove bob@acme.com    # their tokens and sessions stop working at once

# a token for a pipeline or a person (printed once)
hive token create --label ci --subject ci-bot --org acme --role member --project radar-x --expires-days 90 --rate-per-min 600
hive token list ; hive token revoke ci
```
Roles: `viewer` reads; `member` reads and writes their own notes; `editor` may edit any note in scopes they belong to;
`admin` is an editor who approves everything in the organisation. Teams and projects can also be synchronised from
Google Workspace/Entra groups ([GROUPS](GROUPS.md)).

## 6. Day-to-day operation

| I want to ... | Do this |
|---|---|
| see approvals waiting | admin UI `/admin/promotions` (needs `HIVE_ADMIN_UI=true`) or `kb_list_promotions` |
| see who did what | `/admin/audit`, `kb_admin_audit` |
| see usage and quotas | `/admin/usage`, `/admin/activity` |
| import a handbook/wiki/repo | `HIVE_CONNECTORS=[...]` ([CONNECTORS](CONNECTORS.md)); `hive connectors run`; `hive ingest-git`; `hive ingest ./docs` |
| catch up | `kb_digest`, weekly email (`HIVE_DIGEST_WEEKDAY`, SMTP), `hive digest --user ann@acme.com` |
| find old or duplicate notes | admin UI `/admin/stale`, `/admin/duplicates`; tools `kb_review_stale`, `kb_similar`, `kb_merge` |
| delete old things automatically | `HIVE_RETENTION_RULES=[{"kind":"session","days":90,"action":"archive"}]`; preview with `hive retention` |
| erase a person | `hive forget ann@acme.com --include-shared --erase-audit --yes` |
| back up / restore | `hive backup`, `hive backup-verify`, `hive restore --force` (server stopped) |
| move between SQLite and Postgres | `hive migrate to-postgres --database-url ...` / `--embedded` / `hive migrate to-sqlite --data-dir ...` ([MIGRATE](MIGRATE.md)) |
| change the embedder | change `HIVE_EMBEDDER`, run `hive reindex --org acme`, then `hive vectors prune` |
| measure search quality | `hive eval evals/core.json`, or build a set from real searches with `HIVE_SEARCH_FEEDBACK=true` + `hive eval-build` |

## 7. Use cases

### 7.1 A solo developer with Claude Code
*Goal: stop re-explaining the same project to the agent every week.*
1. Run the container (2.2) and create a token with your own project.
2. `hive-hooks init --url http://localhost:8080/mcp --editor claude --save-token`.
3. Work as usual. At session start the agent sees the project's notes; when it finds the cause of a flaky test it saves a
   `howto`; at session end a report is stored. Next week's session starts with that knowledge.

### 7.2 A small team sharing a server
*Goal: what one person's agent learns is available to the others.*
1. Deploy one container on a small VM or Azure (2.4) with `HIVE_PUBLIC_URL` and `HIVE_SECRET_KEY`.
2. Add the people (`hive user add`), give each a token or Google sign-in, put them all in the project `radar-x`.
3. Agents save with `share_with_project=true`; everyone's consult results now include the project's notes.

### 7.3 Getting a practice adopted organisation-wide
*Goal: the release checklist should be the official one.*
1. Ann saves the checklist in her project. 2. She calls `kb_request_promotion(id, to="org", owner="acme")`.
3. Tina, listed as approver (or any admin), sees it at `/admin/promotions` (and in Slack/Teams/email), reads the text,
   approves. 4. The server applies the change (if the note was edited meanwhile the request becomes *stale* and must be
   re-requested). 5. The checklist now appears for everyone, as organisation-wide knowledge, ranked slightly below their own
   project's notes.

### 7.4 Claude.ai / ChatGPT for non-developers
*Goal: managers can ask "what did we decide about the vendor change?".*
Enable Google sign-in with the company domain, add the managers to the directory as `viewer`/`member`, add the connector in
claude.ai. They get consult and read tools, see only what they are entitled to, and cannot be impersonated by arguments.

### 7.5 Questions and capture in Slack
*Goal: knowledge that lives in chat does not get lost.*
Create the Slack app ([SLACK](SLACK.md)), enter workspace id, signing secret and bot token on `/admin/integrations`. People type
`/hive how do we rotate the API keys?` (private answer) or `/hive save <text>` (private note), or use the *Save to Hive*
message shortcut. In a channel, `@hive` answers only with organisation-wide knowledge and the channel's mapped project.

### 7.6 The same in Microsoft Teams
Register the bot ([TEAMS](TEAMS.md)), run `hive teams-package --app-id <id>`, upload the zip. People chat with the bot,
use **Search Hive** in the compose box (cards of shared knowledge), **Save to Hive** on any message (private or into one of
their projects), and pasted note links unfurl into cards.

### 7.7 Bringing in the existing handbook
Set `HIVE_CONNECTORS='[{"name":"handbook","type":"confluence","base_url":"https://acme.atlassian.net/wiki","space":"ENG","email":"bot@acme.com","token_env":"CONF_TOKEN","org":"acme","owner":"connector-bot","project":"eng","every_minutes":360}]'`.
Pages become notes in project `eng`, are re-imported when they change (old versions archived), and archived when
they vanish — unless more than half disappear at once, which pauses the sync until an admin runs `hive connectors run --force`.

### 7.8 Keeping the knowledge trustworthy
Turn on the weekly digest and `HIVE_STALE_REVIEW_DAYS=180`. Each Monday people get "what's new for you" and "still true?" for
their old notes; `kb_confirm` refreshes a note, `kb_update` fixes it, `kb_conflicts` shows near-identical notes that
disagree, `kb_merge_propose` drafts a merged note with an optional language model (only notes at or above `HIVE_LLM_MIN_SCOPE`
are ever sent to it).

### 7.9 Privacy request or offboarding
`hive forget ann@acme.com --yes` (private notes), add `--include-shared` for everything she wrote, `--erase-audit` to replace her id
in the audit trail with a random alias; her tokens stop working at once if she is removed from the directory.

### 7.10 Moving up from SQLite to PostgreSQL
Stop the server, `hive backup`, then `hive migrate to-postgres --embedded` (inside the Postgres image) or `--database-url ...`.
The command copies everything, proves the copy, builds the indexes; start the server with the new settings.

### 7.11 Running it for a regulated team
Fixed `HIVE_SECRET_KEY`, `HIVE_REPLICAS` > 1 on external PostgreSQL, SSO via Entra, `HIVE_ADMIN_UI`, rate limits, retention rules,
audit review, `EMBEDDER=onnx` (nothing leaves the server) and no LLM; see [HARDENING](HARDENING.md) and [OBSERVABILITY](OBSERVABILITY.md).

## 8. When something looks wrong

| Symptom | Check |
|---|---|
| agent says unauthorized | token revoked/expired, person removed from the directory, wrong `HIVE_PUBLIC_URL` (Host/Origin checks) |
| search finds nothing relevant | `hive eval`; with the `hashing` embedder only shared words match; check the relevance floor `HIVE_RELEVANCE_FLOOR` |
| a note is missing for others | it is private or project-scoped: `kb_get` shows the scope; request promotion |
| `indexed: false` after saving | the embedder was unavailable; a job retries (see `/metrics`, logs `job ... failed`) |
| server will not start: "another instance is using this backup directory" | another container holds the lease; stop it or wait for its shutdown |
| readiness 503 | starting or draining; or embedded PostgreSQL stopped (restart the container) |
| Slack/Teams says it cannot match you | add your work email to the directory (`hive user add`) |
