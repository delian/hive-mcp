# hive-mcp: Requirements Specification

Status of this document: reconstructed from the development conversation (23-27 October 2026) and from the
repository at the time of writing. It is written so that an engineer, or an AI, who has **only this document and its
companion, [REQUIREMENTS-ANNEX.md](REQUIREMENTS-ANNEX.md)** (the exact technical catalogue), can build an equivalent
system. Where the repository and this document disagree on behaviour, the behaviour documented here and in the
feature pages under `docs/` is the intent; accidental behaviour is not a requirement.

<!-- OUTLINE (filled in order): 0 How to read - 1 Vision/users/glossary - 2 Ledger - 3 Functional - 4 Non-functional -
     5 Technology - 6 Non-goals - 7 Open questions - 8 Traceability -->

---

## 0. How to read this document

### 0.1 Requirement identifiers

Every requirement has an identifier `REQ-<AREA>-<nn>`. The area codes are:

| Area | Meaning |
|---|---|
| KNO | Knowledge model: items, kinds, scopes, tags, revisions, status, idempotency |
| ACC | Access control, roles, promotion and approvers |
| SRCH | Search and ranking (keyword, semantic, rerank, feedback, nudges) |
| EMB | Embedding models and the privacy rule for hosted embedders |
| SESS | Sessions, compaction reports, agent hooks and client installation |
| GRAPH | Links between items |
| ENT | Entities |
| HIST | Item history (revisions) |
| ING | Ingestion of files, URLs, git, OCR; content screening |
| CUR | Curation: tags, duplicates, merge, stale review, conflicts, digest |
| RET | Retention, purge, erasure |
| CONN | Scheduled connectors |
| LLM | Optional language model |
| AUTH | Authentication: static tokens, OAuth, SSO, directory, groups |
| ADM | Web admin pages and admin tools |
| SLK | Slack front door |
| TMS | Microsoft Teams front door |
| NOT | Notifications |
| USE | Usage, quotas, rate limits |
| OBS | Observability |
| OPS | Operations: backup, restore, lease, hand-off, migration, embedded Postgres, deployment |
| CI | Continuous integration and security tooling |
| MCP | The MCP interface as a whole (tools, prompts, resources, errors, instructions) |
| NFR | Non-functional requirements (sub-areas SEC, REL, PERF, QA, DOC are written `NFR-SEC-nn` etc.) |
| TECH | Technology and packaging constraints |

### 0.2 Priority

The key words **MUST**, **SHOULD** and **MAY** are used as in RFC 2119.

- **MUST** - the system is not equivalent without it. A regenerated system that lacks it is wrong.
- **SHOULD** - expected; a regenerated system may deviate only with a recorded reason.
- **MAY** - optional, or off by default; if built it must follow the stated behaviour.

### 0.3 Origin tags

| Tag | Meaning |
|---|---|
| **[U]** | Stated or chosen by the user (Delian). Where the date and order in the conversation are known they are given as `U<n>`, where `U<n>` is the entry number in the ledger of section 2 (for example `[U3]`). Answers given through the question tool are also `[U]`. |
| **[D]** | A design decision taken by the assistant and accepted, or not objected to, by the user. The user routinely replied with "yes", "go ahead" or a numbered list, so most engineering detail is `[D]`. |
| **[A]** | An assumption made in order to proceed. It was never confirmed, and is collected in section 7 for confirmation. |

A requirement may carry more than one tag, for example `[U3][D]`: the user asked for the capability and the assistant
chose the shape. Acceptance criteria are written so that each can be turned into an automated test.

### 0.4 Companion document

[REQUIREMENTS-ANNEX.md](REQUIREMENTS-ANNEX.md) holds every exact value: tool signatures, HTTP routes, CLI commands,
environment settings, database schema, constants, token formats, card and manifest formats and deployment artefacts.
A requirement here that says "see Annex A.3" is incomplete without it. Annex references use the form `Annex <letter>.<n>`.

### 0.5 Conventions

- "The server" is the running `hive-mcp` process. "The client package" is `hive-hooks`.
- "Org" is an organisation (tenant). "Item" is one unit of knowledge (a note).
- All timestamps are UTC and timezone-aware. All identifiers in URLs, tokens and IDs are opaque strings.
- "Secret" means a credential or key. "Content" means the title or body of an item.
- Times such as "5 minutes" are exact defaults unless a setting is named.

---

## 1. Vision, users and roles

### 1.1 Vision

`hive-mcp` is a **remote MCP (Model Context Protocol) server that acts as shared team memory**. AI coding agents and
assistants report session logs, context compactions, research, formulas, measurements, decisions and best practices to it,
and consult it before starting work, so that what one member of a team learns is found by every other member's agents
("as one big swarm"). It also accepts documents. `[U2]`

The motivating example given by the user: a team separately working on a hardware project, producing research,
measurements, tests, code, product and project-management material and best practices through many different members. That
knowledge should be shared, tagged, searched and used automatically to accelerate the work. `[U2]`

Key properties asked for by the user:

1. **Remote**: it works as if running on a remote machine; clients are MCP clients (claude.ai, ChatGPT, Gemini, DeepSeek-style
   web assistants, coding agents on laptops, pipelines). `[U2][U3]`
2. **Private by default, shared by promotion**, with groups, teams and projects; other groups' knowledge can be referenced but
   is always labelled as coming from elsewhere. `[U3]`
3. **Soft upgrades**: a restart of a few seconds is acceptable, as long as no client has to be restarted. `[U2][U3]`
4. **Scale** of gigabytes, "maybe 100 GB". `[U3]`
5. **One container**: everything must be able to run in a single Docker container, locally or as a single-container cloud
   app (Azure first). `[U5][U34]`
6. **Engineering quality**: Python best practices, typed code, small files, proactive bug hunting, shared code written once,
   tests and documentation. `[U2]`

### 1.2 Users and the way they use the system

| Actor | How they use it | Interfaces |
|---|---|---|
| **AI coding agent** (Claude Code, Codex, Gemini CLI, Cursor, VS Code agents) | Consults before a task, remembers findings, reports session and compaction summaries; reads notes; links and curates | MCP over Streamable HTTP with a bearer token; Claude Code hooks; an `AGENTS.md` note; editor MCP configs |
| **Cloud assistant** (claude.ai, ChatGPT, Gemini) | Same tools, signed in per person | MCP with OAuth 2.1 (authorization code with PKCE, dynamic client registration), delegating login to Google Workspace / Entra ID / OIDC |
| **Team member (person)** | Asks and saves from where the team talks; reads a note; approves requests | Slack (`/hive`, message shortcut, mentions); Microsoft Teams (bot, message extension, link unfurling); web pages under `/admin`; weekly digest email |
| **Approver** | Reviews promotion requests into the scopes they approve | MCP tools; `/admin/promotions`; notifications |
| **Admin** | Manages people, tokens, integrations, quotas, audit, connectors | `/admin/*` pages; `kb_admin_*` tools; CLI |
| **Operator** | Deploys, backs up, restores, migrates, evaluates | `hive` CLI; Docker; Bicep; CI; environment settings |
| **Pipeline / service account** | Pushes and reads with a static token | MCP with a `hive_` bearer token, optional rate limit |

### 1.3 Roles (authorisation levels)

Four roles, ordered: `viewer < member < editor < admin` (REQ-ACC-02). The role of a person comes from the user directory
(or from the claims of a static token); it is never supplied by a tool argument.

### 1.4 Glossary

| Term | Definition |
|---|---|
| **Item** | The unit of knowledge: id, org, kind, title, body, scope, visibility, status, tags, revision, author, timestamps, optional `supersedes` and `source`. |
| **Kind** | One of: note, research, formula, measurement, decision, best_practice, doc, howto, session, compaction, entity. |
| **Scope** | Where an item lives: `private` (one user), `project`, `team` or `org`, with an owner id. Ordered narrow to wide: private < project < team < org. |
| **Visibility** | `scope` (only members of the scope) or `org` (members see it as primary, everyone else in the org sees it as *referenced*). |
| **Tier** | `PRIMARY` (the item is inside one of the caller's own scopes) or `REFERENCED` (org-visible, but from a scope the caller does not belong to). Referenced results are labelled `EXTERNAL`. |
| **Promotion** | Sharing wider than a project. A request that an approver decides (approve, reject), the requester can withdraw. |
| **Approver** | An admin (any scope in the org), or a non-admin member named approver of specific scopes. |
| **Principal** | The authenticated caller: id, org, role, teams, projects, approver scopes. Built only from verified token claims. |
| **Directory** | The user directory (`users.json`): who may sign in and what each person belongs to. |
| **Static token** | A `hive_`-prefixed bearer token for pipelines and the CLI; only its hash is stored. |
| **Embedder** | The component that turns text into a vector. Kinds: hashing (dev), local ONNX, hosted OpenAI-compatible. |
| **Relevance floor** | The per-embedder minimum cosine similarity below which a vector hit is dropped. |
| **Referenced / external** | Knowledge from a group the caller does not belong to. |
| **Profile** | `embedded` (SQLite + files, one volume) or `external` (Postgres + pgvector). |
| **Port / adapter** | The storage interfaces (`ports`) and their implementations (`adapters`: memory, SQLite, Postgres, LanceDB vectors). |
| **Conformance suite** | One test suite run against every storage backend; the executable specification of the ports. |
| **Job** | A durable background task (index an item, apply a promotion, notify, purge cleanup) with a lease and retries. |
| **KV store** | A small expiring key-value store with atomic `take`, used for OAuth state, sessions, claims and caches. |
| **Front door** | A non-MCP way in: Slack, Teams, the admin web pages. |
| **Connector** | A scheduled import that keeps a project's knowledge equal to an outside source. |
| **Lease** | A file in the backup directory that lets exactly one server instance own the data. |
| **Hand-off** | The protocol by which a newer revision asks the running one to take a final backup and exit. |
| **Audit event** | An append-only record `(seq, org, actor, action, target, time, details)` containing ids and short facts, never content. |
| **Annex** | REQUIREMENTS-ANNEX.md. |


---

## 2. Chronological ledger of the user's requests

This section records, in order, every genuine user message that stated a requirement, preference, constraint, decision or correction, what the assistant asked or answered, and the requirement identifiers it produced.
Messages that carry no requirement are listed briefly so that the order is complete. Automated messages are **not** user input and are excluded: the 12 "Stop hook feedback" notices (`stop-hook-git-check.sh`, demanding commit and push), the worker-restart notice,
background-task and monitor notifications, and subagent hand-backs. Sources: the conversation transcript (55 user-role entries in the transcript body: 12 stop-hook notices and 1 restart notice leave 42 genuine messages; two further messages the user typed while the assistant was working, U37 and U39, exist only as queued commands; 44 genuine messages in total),
5 question-tool exchanges whose answers count as user decisions (U1, U4, U38, U42, U43; U1 was left unanswered), the four compaction summaries (used as a cross-check) and the repository. Times are UTC.

### 2.1 Standing preference (outside the conversation)

**U0** `[U]` - the user's saved account preference, present in every session of the transcript: *"Ask clarifying questions, do thorough research and then answer"* (sic: "thorrow"). The assistant's practice that followed: research first (MCP spec, SDK, DuckDB, pgvector, OAuth for assistants, Tailscale; benchmarks measured, not assumed), put open decisions to the user with the question tool (U1, U4, U38, U42, U43),
state caveats and "what I could not verify" in every report, and otherwise proceed with sensible defaults when the user gave a go-ahead list (a recorded `[A]` assumption where the default was not confirmed). Produces: REQ-NFR-QA-05, REQ-NFR-DOC-04, section 7.

### 2.2 Ledger

**U1** - 2026-10-23 17:31 - The first message is a start of a sentence followed by keyboard noise: *"I want to make (in python) an MCP server fwhich will work as a remote data ..."*. The assistant asked four questions (what data to expose; transport; auth; read-only or read/write). The user did not answer them; he sent U2 instead, and the answers were treated as given by U2.
Produces: nothing by itself (the assistant's defaults after U2: Streamable HTTP, bearer + OAuth, read and write).

**U2** - 2026-10-23 17:48 - The foundational request (quoted in full; a stray `]=` precedes it in the transcript):

> I want to make (in python) an MCP server which will work as a remote mcp server and will preserve knowledge asking the coding or the llm agent to report session logs, compacts, researches, important knowledge, to retrieve, consult, with the knowledge database of this mcp server. All have to be able to work remotely (as if the MCP server is running on a remote machine) - so mostly with communication to the MCP client. It should support soft upgrades (research how to do it) so if we update data, or the backend of the MCP server the clients wont lose connectivity or they might be asked to reconnect. The data shall support cataloging (research best practices), ability for the agent automatically to ask about it, maybe tags (aka - data for programming, for research, formulas, radar stuff), retagging and if decided summarized, updated, reorganized by the agent if we ask the agent to do it for us. It shall be possible to add (cli tool, config - directory, or via the agent) files to be scanned, tokenized, and supporting graph knowledge data of any sort. We imagine the users to connect to this agent and share, implicitly or explicitly knowledge, formulas, discoveries, research, memory, best practices, documentation, and respectively in the opposite direction agents should be possible to automatically consult with the agents and to reuse group accumulated knowledge automatically, in coding, research or other tasks. For example if a team separately work on a hardware project and there is research, measurements, tests, coding, product, project management, best practices, documentation implemented or executed by many different members of the team, this knowledge should be automatically shared, tagged, searched and used, to accelerate the work as one big swarm - research for best practices how that could be implemented. Also follow the best practices to store research, graph data, maybe duckdb, search, retrieve, integrate with agents, etc. The data should also be able to be tagged and searched and retrieved based on relative distance - like the most relevant for the topic, user, project, company or general data. Check how the mcp agent could accelerate or store, or separate, or recognize, or import, or export best the data for the user, and make me a plan for the development. for the python and mcp use the best practices, ruff, uv, typed python, unit tests, documentation, etc. Make every code implementing similar functions to be separated into a common mode so we do not implement twice the same thing, and prevent increase of the bug surface. Try to keep all the python files very small, like 8k max, and if they get bigger split them in modules. try to think proactively for bugs, and to correct them

Assistant response: researched and wrote `PLAN.md` (sections 1-12): MCP spec 2026-07-28 is stateless, SDK v2, hybrid retrieval with RRF, episodes/items/entities/edges, scope-distance ranking, hooks plus instructions for "automatic" use, risk register, phases 0-7, and seven questions.
Produces: REQ-KNO-01..16, REQ-SRCH-01..06, REQ-SESS-01..04/06/10/11/15, REQ-GRAPH-01, REQ-ING-01/13, REQ-OPS-01/02, REQ-ACC-14, REQ-NFR-QA-01..05, REQ-NFR-DOC-01/03, REQ-TECH-01/04, REQ-MCP-01, NG-01/02.

**U3** - 2026-10-23 18:14 - Answers to the seven open questions, in one message (quoted in full):

> we expect data in the range of gigabytes, maybe 100GB. Restart in seconds is acceptable as long as the client doesn't have to be restarted itself. we expect claude.ai and others like gemini, chatgpt, etc to be able to connect online, in addition to coding agents, running at computers, or pipelines, etc. we want to support as automated and remote work as possible, so I guess hosted embedding service and offline should both be supported? the knowledge should be private until promotion, but should be possible to be tagged to groups and or projects, whatever, so users who belong to different groups, teams, projects, should be able to have their own knowledge. Still other teams projects should be able to refer to other's knowledge, if their is missing (but the MCP should explicitly mention it comes from a different group or it is general knowledge) so the users to be informed and if they want they can improve on it in their local context and extend it, or decide to ignore it, for their project. It will run in the cloud like in Azure or in GCP, as a docker container, but it should be possible (for test reasons) to be able to start it locally (in docker container) and we want to have it running into a private network and expose it via cloud tunnel, or tailscale public service, to the cloud llms like claude.ai, chatgpt, gemini, deepseek, etc and they should be able to connect. yes, 8-10kb per file, as an aim, but if you think 8kb is too low, what you would think is justified? 15kb? 20kb?

Assistant response (accepted): storage moved from DuckDB to **Postgres + pgvector** for scale (DuckDB single writer, experimental vss); OAuth 2.1 + PKCE + dynamic client registration for cloud assistants; two retrieval tiers (primary and referenced/external, always labelled; adopt, extend or ignore); embeddings pluggable with a rule that private scopes can forbid hosted providers; file size target 8 KB with **16 KB hard CI limit** and 12 KB warning.
Produces: REQ-NFR-PERF-01, REQ-ACC-01/15, REQ-KNO-03/09, REQ-EMB-01/05, REQ-SRCH-07, REQ-AUTH-01/05, REQ-OPS-01/10/13, REQ-NFR-QA-01, REQ-NFR-REL-02, REQ-NFR-SEC-01/09, REQ-LLM-02.

**U4** - 2026-10-23 18:16 - *"ask me the questions"* (the standing preference in action). Question-tool answers (decisions): data size **"Mixed / not sure"** (size for the worst case, all text embedded); first cloud **Azure**; identity provider **Google Workspace**; promotion to team level and above **"Approver required (Recommended)"**.
Assistant response: recorded in `PLAN.md` sections 13-14: Azure Container Apps + managed Postgres; Google has no dynamic client registration, so an authorization server with DCR in front (later: built in, see U5); approver workflow with a review queue, redaction re-run at submit and approval.
Produces: REQ-ACC-06..13, REQ-AUTH-05..07, REQ-OPS-12, REQ-TECH-06, REQ-NFR-PERF-01.

**U5** - 2026-10-23 18:19 - *"Make all to be able to be started into a single docker container, as having it running locally or as azure app, or container, will be easier"*.
Assistant response: `PLAN.md` section 15: one image, one container; two storage profiles chosen by config (`embedded` SQLite+files default; `external` Postgres+pgvector); built-in OAuth authorization server delegating login to Google (vetted libraries, no hand-rolled crypto); optional extras; Azure Files caveat for SQLite.
Produces: REQ-OPS-10, REQ-OPS-14, REQ-TECH-05/06, REQ-AUTH-05, REQ-NFR-QA-04.

**U6** - 2026-10-23 18:21 - *"yes, where are you storing the code?"* (yes: start Phase 0, the repo skeleton: uv, ruff, mypy, pytest, file-size check, Dockerfile, minimal server). The assistant built it in `/home/user/hive-mcp` and said the container is temporary and needs a remote.
Produces: REQ-TECH-01/02, REQ-CI-01/02, REQ-OPS-16.

**U7** - 2026-10-23 18:28 - *"what remotes are available?"* (the assistant listed the user's GitHub repositories and proposed creating `delian/hive-mcp`). Produces: nothing.

**U8** - 2026-10-23 18:31 - *"pull, merge (overwrite the README if needed) and push to this repo - delian/hive-mcp"*. The assistant merged, replaced the README and pushed to `main`. Produces: REQ-TECH-06.

**U9** - 2026-10-23 18:32 - *"yes"* (to: protocol tests for both MCP eras and real drain logic). Produces: REQ-AUTH-01, REQ-OPS-01, REQ-MCP-05.

**U10** - 2026-10-23 18:34 - *"test both separately to see which performs better"* (the assistant had asked whether to install Postgres for the benchmark or limit it to embedded options). Result: pgvector vs LanceDB vs DuckDB vs sqlite-vec at 1M synthetic chunks, then at 220k real embeddings (bge-small over Simple English Wikipedia). Produces: REQ-NFR-PERF-02, REQ-TECH-05, REQ-EMB-11.

**U11** - 2026-10-23 19:11 - *"rerun the real embeddings"* (re-run the benchmark with real embeddings). Produces: REQ-NFR-PERF-02/04.

**U12** - 2026-10-23 20:31 - *"There has been a merge of the change request made at github - pull and check for changes"*. A Copilot-authored prototype (single-file stdio server) had been merged; the assistant found a lock crash, protocol-version echo and ASCII-only search defects and offered three options. **U13** - 20:41 - *"Option 1"* (remove the prototype). Produces: REQ-SRCH-02 (Unicode search), REQ-ACC-14 (no unauthenticated shared-file model).

**U14** - 2026-10-24 12:41 - *"have you commit everything to the repo?"* (yes; raw benchmark data not in git). Produces: nothing.

**U15** - 2026-10-24 12:42 - *"start building the storage interfaces"*. Delivered: domain model, six ports, in-memory reference adapter, one conformance suite. Produces: REQ-ACC-03..05, REQ-KNO-06..08/12, REQ-EMB-06, REQ-OPS-02, REQ-NFR-QA-04.

**U16** - 2026-10-24 12:59 - *"do it in the order you suggested"* (SQLite adapter, then Postgres adapter, then services/tools). Delivered SQLite + filesystem blobs, validated by deliberate fault injection (11 mutants). Produces: REQ-TECH-05, REQ-OPS-15, REQ-NFR-QA-05.

**U17** - 2026-10-24 16:40 - *"yes go ahead"* (Postgres + pgvector adapter). Found by running against real Postgres: NUL characters, lock keys, import placement. Produces: REQ-KNO-01 (NUL rule), REQ-OPS-15, REQ-NFR-PERF-04.

**U18** - 2026-10-24 17:08 - *"start the next work"* (services and the first MCP tools: remember, consult, report session). Produces: REQ-KNO-09, REQ-SRCH-01..10, REQ-SESS-01..04, REQ-ACC-06, REQ-EMB-09.

**U19** - 2026-10-24 20:45 - *"do the oauth authorization now"*. Delivered the OAuth 2.1 authorization server with Google login, consent, single-use codes, rotation, user directory, `hive user`; tested against a fake Google with real signing and PKCE; 18 deliberate breakages (16 caught). Produces: REQ-AUTH-02..13, REQ-NFR-SEC-08.

**U20** - 2026-10-25 08:19 - *"Go ahead"* (real embeddings: offline ONNX plus hosted adapter; `embed_query`; relevance floor). Produces: REQ-EMB-01..05/08/10.

**U21** - 2026-10-25 08:36 - *"What are the next steps"*. The assistant listed: (1) `hive reindex`, (2) Docker image and Azure files, (3) approval workflow, (4) real-world verification, (5) ingestion and graph, (6) hardening. **U22** - 08:38 - *"Do 1, then 3, then 5 then 6"*: reindex; approval workflow with approvers, crash safety, notifications later; ingestion (text, markdown, HTML, PDF) and graph; hardening
(rate limits on registration, login and MCP; remembered consent; no private text to hosted embedders; the earlier test-stall check). Produces: REQ-EMB-07, REQ-ACC-07..13, REQ-GRAPH-01..06, REQ-ING-01..04, REQ-USE-03, REQ-AUTH-09, REQ-EMB-05.

**U23** - 2026-10-25 13:33 - *"Yes go ahead with that"* (the assistant had offered to take on the Docker image and the Azure deployment files). Delivered: multi-stage Dockerfile, compose file, Bicep, smoke test, GitHub CI (first green run covered tests with Postgres, image and Bicep). Produces: REQ-OPS-10/12/13/16, REQ-CI-01..05.

**U24** - 2026-10-26 11:39 - *"what other features are there to develop outside of the real-life tests?"* (the assistant answered with a 20-item list). **U25** - 11:50 - *"do 1, then 2, then 3, then 4, then 5"* = (1) curation: `kb_retag`, `kb_rename_tag`, `kb_similar`, `kb_merge`; (2) MCP prompts and resources; (3) browse: `kb_list`, `kb_tags`; (4) approver ergonomics: per-scope approvers, crash-safe approval, notifications; (5) deletion and retention: `kb_purge`, `hive forget`, retention. Produces: REQ-CUR-01..04, REQ-SESS-16/17, REQ-KNO-13, REQ-ACC-08/11, REQ-NOT-01/02, REQ-RET-01..03/05.

**U26** - 2026-10-26 12:20 - *"what are the other open work items, before we get to real life integration or deployment, we could work on?"* (a 21-item list, A-D). **U27** - 12:52 - *"do 1, 2, 3, 5, 13, 16, 17, 18, 19, 20, 21"* = (1) search-quality evaluation with a proper set; (2) old-model vector cleanup and reindex progress; (3) backup and restore; (5) refresh stale docs; (13) admin tooling: audit query, usage, quotas;
(16) graph-aware retrieval; (17) entity layer; (18) revision history; (19) multilingual model and reranker; (20) more input types: Office, OCR, URLs, Git; (21) small web admin page (approval queue, audit log). Delivered, except OCR (no engine at the time; done later at U32). Produces: REQ-SRCH-11..13, REQ-EMB-03/07, REQ-OPS-04, REQ-ADM-01..04/09, REQ-USE-01/02, REQ-ENT-01..06, REQ-HIST-01..04, REQ-ING-02/05/06/07.

**U28** - 2026-10-26 14:46 - *"what are now the other open tasks we could work on?"* **U29** - 14:48 - *"do 5, 6, 7, and 9 now, then 10, 12, 13, 14, 16"* = (5) multi-replica safety; (6) Google Workspace group sync; (7) observability (metrics, structured logs, alerts); (9) a security review pass plus dependency and image scanning in CI; (10) audit log newest-first; (12) search evaluation on real data (opt-in feedback, `eval-build`, `eval --recommend`);
(13) LanceDB vector adapter; (14) agent onboarding (Claude Code hooks, AGENTS.md); (16) digest and notifications. Produces: REQ-OPS-14, REQ-AUTH-14, REQ-OBS-01..04, REQ-NFR-SEC-12, REQ-CI-03, REQ-ADM-04/09, REQ-SRCH-14, REQ-EMB-11, REQ-SESS-06..11, REQ-CUR-08/11.

**U30** - 2026-10-26 17:54 - *"what is the status?"* (all nine done, CI green; unverified against real systems). **U31** - 17:54 - *"what other work items we can work on?"* (a 14-item list). **U32** - 17:56 - *"do 7, 9, 10, 11, 12, 13, 14 and then 5"* = (7) approximate index for the LanceDB store, verified on real embeddings; (9) revocable server-side admin sessions; (10) token and user management on the admin page; (11) promotion history page; (12) better hook summaries; (13) digest items edited in the window; (14) slim installable hooks client; then (5) OCR for scanned PDFs and images.
Produces: REQ-EMB-11, REQ-ADM-02/03/05, REQ-SESS-09/13/14, REQ-CUR-09, REQ-ING-08.

**U33** - 2026-10-27 07:39 - *"what more we can build ?"* (a 16-item list in four groups A-D). **U34** - 07:42 - the instruction (quoted in full):

> do 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, for 16 - we want all to be able to run into a single container, not docker-compose with many containers, to be able to deploy it as a single container app in the cloud or to be able to run it locally with docker

= (5) Slack capture; (6) stale-note review; (7) search feedback loop; (8) duplicate and merge suggestions; (9) project digests; (10) connectors (Drive, GitHub, URLs); (11) retention rules and "forget a person"; (12) usage report and per-token limits; (13) content safety on ingest (secrets, hidden characters, injection flags); (14) SSO beyond Google (Entra, generic OIDC); (15) Dependabot PRs; (16) **single-container** mode with self-backup, lease, `hive demo` and a one-container Azure template instead of a multi-service compose.
Produces: REQ-OPS-05/06/10/11/12, REQ-SLK-01..05, REQ-CUR-05/06, REQ-SRCH-14, REQ-CUR-10, REQ-CONN-01..05, REQ-RET-05, REQ-USE-04/05, REQ-ING-12..14, REQ-AUTH-08, REQ-CI-03, REQ-SESS-05.

**U35** - 2026-10-27 08:52 - *"what other features and work we can do to the code?"* (a list with groups A real-system checks, B gaps, C product features, D platform). **U36** - 08:53 - *"do all B, C and D"* = B5 retention that can archive; B6 full person erasure; B7 Microsoft Teams front door; B8 Slack threads and mentions; B9 admin pages for connectors, stale notes, duplicates and a per-token rate field;
C10 merge assist with an optional LLM; C11 better ranking signals; C12 conflict detection; C13 more connectors (Confluence, Notion, Jira, SharePoint/OneDrive); C14 per-session "what I used" view; C15 notification routing to Slack/Teams; D16 optional PostgreSQL inside the single image; D17 zero-downtime hand-off for the one-container mode; D18 mutation testing in CI; D19 second security pass and fuzzing; D20 client installer / `AGENTS.md` generator (IDE extensions declared out of scope).
Produces: REQ-RET-04/05, REQ-TMS-01..04, REQ-SLK-04/05, REQ-ADM-06, REQ-LLM-01..04, REQ-SRCH-15, REQ-CUR-07, REQ-CONN-03, REQ-SESS-05, REQ-NOT-03, REQ-OPS-07/08, REQ-CI-06/07, REQ-SESS-12, NG-03.

**U37** - 2026-10-27 08:54 (mid-turn) - *"regarding the teams on slack, could that be a configurable parameter, that the users could manage via the admin interface, or authenticated mcp chat command?"* Interpreted (an `[A]`, see OQ-09) as run-time, admin-managed integration settings: stored settings beat environment, secrets only on the admin page, tools for non-secret settings, audited, effective within seconds.
Produces: REQ-SLK-06, REQ-ADM-06, REQ-ADM-09.

**U38** - 2026-10-27 10:47 - *"ask me the questions"* (the assistant had finished the A-D list and had open decisions). Answers: **PyPI publishing: "Yes, set it up"** (documented steps and an opt-in release workflow; the pypi.org part is the user's); **next step: "Evaluate LLM and ranking"**; **embedded-Postgres supervision: "No, keep it simple (Recommended)"** (no watchdog; readiness fails and the platform restarts the container).
Produces: REQ-SESS-14, REQ-LLM-05, REQ-OPS-08, NG-06.

**U39** - 2026-10-27 10:48 - *"is the python code, dependencies and environment managed by uv in this project?"* (answer: yes; uv workspace, lockfile, `uv sync`, `uv run`). Produces: REQ-TECH-01/02.

**U40** - 2026-10-27 11:48 - *"run eval-assist yourself"* (no provider key existed; the exact judge and merge prompts were answered by a Claude model through a subagent and scored by the `eval-assist` code; results written to `docs/LLM.md`). Produces: REQ-LLM-05.

**U41** - 2026-10-27 13:11 - *"What is the rest of the work we could do?"* **U42** - 13:13 - (quoted):

> Do these:
>
> Teams message extension: the Teams front door only does chat commands today.
> SQLite → Postgres migration: archives from the two profiles can't be swapped. I'd add a hive migrate command.
>
> and ask me the questions for the decisions to me

Question-tool answers: **ranking votes: "Drop votes, keep confirmation bonus (Recommended)"**; **mutation testing scope: "OAuth and token code" and "Teams verification and connector safety rules"** (both ticked); **Teams message extension actions: "Save message to Hive (action command)", "Search Hive (search command)", "Both (Recommended)"**; **`hive migrate`: "Both directions, via a verified archive (Recommended)"**.
Produces: REQ-SRCH-15, REQ-CI-06, REQ-TMS-05/06/09, REQ-OPS-09.

**U43** - 2026-10-27 15:45 - (quoted):

> do that:
> The privacy and terms URLs in the Teams manifest need real pages before you publish beyond your own tenant.
> No form for choosing a project when saving a Teams message.
> No link unfurling in Teams.

Question-tool answers: **note links: "New read-only note page /n/<id> (Recommended)"** (implemented at `/admin/n/<id>`, any signed-in directory user, same access rules as `kb_get`); **legal pages: "Built-in plain pages + override URLs (Recommended)"** (`/legal/privacy`, `/legal/terms`, `HIVE_LEGAL_ORG`, `HIVE_LEGAL_CONTACT`, `HIVE_PRIVACY_URL`, `HIVE_TERMS_URL`; not legal advice);
**save-form: "Only if the user has projects (Recommended)"**. Produces: REQ-ADM-07/08, REQ-TMS-06/07/09.

**U44** - 2026-10-27 16:20 - the final request (quoted in full):

> review carefully the code for bugs, make tests, regression tests for every bug you found, and stress tests, to make sure it is supposed to work up to the requirements. Write a documentation - with readme, instructions how to use, use case, and separate document with architecture, and separate document with the user requirements accumulated over the development, in such a way, that only based on the requirements data, this code would be able to be regenerated

Produces: REQ-NFR-QA-05/06, REQ-NFR-DOC-02, REQ-CI-08 and this document (with its Annex). While this specification was written, other agents were fixing bugs in `src/`; this specification describes documented intent, not accidental behaviour.

**U45** - 2026-10-29 - *"do 1, 2, 3, 5"* (go-ahead for the assistant's list of open review items: 1 `source` length validation, 2 erasing `scope_owner` of old private revisions, 3 recheck the Annex, 5 a weak regression test).
Produces: the 2,000-character `source` limit (Annex E.3), REQ-RET-04 extension, Annex refresh.

**U46** - 2026-10-30 - *"what more work we could do?"* The assistant listed: verification against real services, search quality on real data, load measurement, smaller product gaps, release and ops.

**U47** - 2026-10-30 - *"do 2,do 3, do 4 now"* (search quality at scale, load and soak measurement, smaller product gaps). The assistant asked the open-question decisions through the question tool; the answers were: **OQ-01 "Accept 15 min (Recommended)"**; **OQ-14 "Add all three"** (fork, ignore, suggest-edit); **OQ-11 "Fail with a clear message (Recommended)"**; **OQ-13/OQ-15 "Build a Parquet/JSONL export" and "Build background bulk ingest"**.
Produces: REQ-ACC-18 (foreign-note tools), `ops/pgclient.py` (REQ-OPS-04), `hive export` (REQ-OPS-17/18), background bulk ingest (REQ-ING-15), `X-RateLimit-*` headers (REQ-USE-03), `scripts/mutation_changed.sh` (REQ-CI-06), the search evaluation at scale (docs/EVALUATION.md) and sizing (docs/SIZING.md).

### 2.3 Go-ahead lists in one table (what each number meant)

| Message | Numbers | What they mean (the assistant's list at that moment) |
|---|---|---|
| U22 (list of U21) | 1, 3, 5, 6 | 1 `hive reindex`; 3 approval workflow; 5 ingestion + graph; 6 hardening |
| U25 (list of U24) | 1-5 | 1 curation; 2 prompts/resources; 3 browse; 4 approver ergonomics; 5 deletion/retention |
| U27 (list of U26) | 1, 2, 3, 5, 13, 16, 17, 18, 19, 20, 21 | 1 search eval; 2 vector cleanup/reindex progress; 3 backup/restore; 5 docs refresh; 13 admin tooling; 16 graph-aware retrieval; 17 entities; 18 history; 19 multilingual + reranker; 20 more input types; 21 web admin page |
| U29 (list of U28) | 5, 6, 7, 9, 10, 12, 13, 14, 16 | 5 multi-replica; 6 group sync; 7 observability; 9 security review; 10 audit newest-first; 12 real-data eval; 13 LanceDB; 14 agent onboarding; 16 digest |
| U32 (list of U31) | 7, 9, 10, 11, 12, 13, 14, 5 | 7 Lance ANN; 9 revocable admin sessions; 10 token/user admin page; 11 promotion history page; 12 hook summaries; 13 digest edits; 14 slim hooks client; 5 OCR |
| U34 (list of U33) | 5-16 | 5 Slack/Teams capture (Slack built; Teams later); 6 stale review; 7 search feedback; 8 duplicates; 9 project digests; 10 connectors; 11 retention + forget; 12 usage; 13 content safety; 14 SSO beyond Google; 15 Dependabot; 16 single container |
| U36 (list of U35) | all B, C, D | B5-B9, C10-C15, D16-D20 (as in the U36 entry) |

### 2.4 Assistant questions the user never answered (carried into section 7)

The seven decisions of the first plan were answered only partly by U3 (clients, scale, hosting, embeddings, sharing default, file size). Later unanswered assistant questions: whether a 15-minute loss window on a hard crash is acceptable (OQ-01); whether the team uses Slack or Teams, Google or Microsoft 365, and whether this is for one team or for customers (OQ-10);
whether a Google Workspace or Entra tenant can be used for `hive doctor`-style checks (the command was not built; NG-09); whether the user will supply a Google OAuth client and a public URL for the real-service trials (OQ-02/03); the choice of PyPI trusted-publisher owner details (the user did the pypi.org part, not recorded).

---

## 3. Functional requirements

Each requirement is a testable statement. "Accept:" lists the observable criteria. Exact values (limits, names, formats)
are in the Annex; the Annex reference is given where it matters. Requirements marked `[U<n>]` trace to the ledger in
section 2.

### 3.1 Knowledge model (KNO)

**REQ-KNO-01** MUST `[U2][D]` **Item.** The unit of knowledge is an *item* with these fields: `id` (32-character lower-case
hex, generated by the server), `org_id`, `kind`, `title`, `body`, `scope` (level + owner), `visibility`, `status`,
`tags` (a set), `revision` (integer, starts at 1), `author_id`, `created_at`, `updated_at` (timezone-aware UTC),
optional `supersedes` (an item id) and optional `source` (free text provenance; at most 2,000 characters on any item, and at most 400 when set by the
built-in writers). A hash of `(kind, title, body)` after Unicode NFC normalisation (`content_hash`) is stored for duplicate
detection.
*Accept:* an item cannot be created with an empty id, org or author; a title that is empty/blank or longer than 200
characters is refused; a body that is blank or longer than 200,000 characters is refused; a NUL character in any text field
(id, org, author, title, body, scope owner, supersedes, source) is refused with a validation error on every backend; naive
(timezone-less) timestamps are refused; a revision below 1 is refused.

**REQ-KNO-02** MUST `[U2][D]` **Kinds.** `kind` is one of: `note`, `research`, `formula`, `measurement`, `decision`,
`best_practice`, `doc`, `howto`, `session`, `compaction`, `entity`. The default kind is `note`.
*Accept:* an unknown kind in any tool argument produces an error that lists the valid kinds.

**REQ-KNO-03** MUST `[U3][D]` **Scopes and visibility.** An item lives in exactly one scope: `private` (owner = user id),
`project` (owner = project id), `team` (owner = team id) or `org` (owner = org id), ordered narrow to wide
`private < project < team < org`. Visibility is `scope` (only members of the scope see it) or `org` (members see it as
primary; every other member of the same org sees it as *referenced*). A private item can never have visibility `org`.
*Accept:* creating or moving a private item with visibility `org` is refused; a scope with an empty owner is refused.

**REQ-KNO-04** MUST `[D]` **Status.** `status` is one of `draft`, `active`, `superseded`, `archived`; the default for a new
item is `active`. `archived` items are hidden from search, lists, digests and the graph; `superseded` items stay searchable
but are down-ranked (REQ-SRCH-06); `draft` items are searchable and slightly down-ranked.
*Accept:* `kb_update status=archived` removes the item from `kb_consult` and `kb_list` (default status filter `current`
= active + draft) and from the text and vector indexes; setting it back to `active` restores it.

**REQ-KNO-05** MUST `[U2][D]` **Tags and the single normaliser.** Tags are normalised by one function used by tools, the CLI,
ingestion, connectors and import: Unicode NFKC, case-folded, runs of whitespace and underscores become `-`, leading/trailing
`-` and `/` stripped, repeated `/` collapsed; the result must match `facet/value` style segments separated by `/`, each segment
made of letters/digits (any script) joined by single `-` or `.`; at most 64 characters per tag and 32 tags per item.
*Accept:* `"Domain/RF Lab"` becomes `domain/rf-lab`; `"../x"`, an empty string, a tag of 65 characters and a 33rd tag on one item
are refused; tags containing control or path-like characters are refused; non-Latin letters (for example Bulgarian) are kept.

**REQ-KNO-06** MUST `[U2][D]` **Retagging is set arithmetic.** Tag changes are expressed as add and remove sets, never as a
whole-list replacement, so two concurrent edits cannot drop each other's tags. A tag cannot be both added and removed in
one patch. An empty patch is refused.
*Accept:* two concurrent `kb_update` calls adding different tags both survive when each carries a current revision
(the loser of the revision race re-reads and retries).

**REQ-KNO-07** MUST `[D]` **Revisions and optimistic concurrency.** Every update, tag change, status change, restore and move
creates a new revision (`revision + 1`, `updated_at` = now). An update must carry `expected_revision`; if it differs from the
stored revision the write is refused with a conflict error that tells the caller to re-read the item (`kb_get`) and retry.
Permission is checked before the revision.
*Accept:* of eight concurrent updates with the same expected revision exactly one succeeds; a caller without write rights
gets a permission error even with a stale revision.

**REQ-KNO-08** MUST `[D]` **Idempotent create.** Creating an item with an `idempotency_key` (1-200 characters, no NUL) is
idempotent per `(org, author, key)`: the same key with the same payload fingerprint returns the original item with
`created=false`; the same key with a different payload fails with an idempotency conflict; a *failed* create does not consume
the key; concurrent creates with the same key create exactly one item.
The fingerprint covers content hash, tags, scope level and owner, visibility, status, supersedes and source.
*Accept:* replaying `kb_remember` with the same key twice stores one item and the second response says an identical earlier
request was replayed; reuse with a different body fails.

**REQ-KNO-09** MUST `[U3]` **Private by default.** `kb_remember` and every writer that has no explicit target stores the item
in the caller's own private scope with visibility `scope`. Nothing becomes visible to anyone else without an explicit
share or an approved promotion.
*Accept:* a second user in the same org, project and team cannot find, get, list or link to an item that the first user
saved without `share_with_project`.

**REQ-KNO-10** MUST `[D]` **Supersession.** An item may be marked `superseded` (status) and linked `supersedes` from the
newer item (REQ-GRAPH-01). Search shows superseded items with lower weight and `kb_consult` warns when a result is
superseded by a newer visible item (REQ-SRCH-13).
*Accept:* `kb_merge` produces a new item linked `supersedes` to each source and marks sources `superseded` (default).

**REQ-KNO-11** MUST `[D]` **Source and stored logs.** `source` records provenance: `file:<name>`, `url:<address>`,
`git:<url>#<path>`, `connector:<name>:<key>`, `slack:...`, `teams:...`, or `blob:<sha256>:<size>` for a stored full log
(REQ-SESS-03).
*Accept:* `kb_get` reports `has_log=true` exactly when the source starts with `blob:`.

**REQ-KNO-12** MUST `[D]` **Keyset pagination.** Listing is newest first and keyset-paginated by `(timestamp, id)` with an
opaque cursor (base64url of the JSON `[iso-timestamp, id]`); there are no repeats or skips even when many items share a
timestamp; a malformed cursor, or one carrying a timezone-less timestamp, is refused. A page holds 1 to 100 items. The
ordering key is creation time, or last-change time when `sort=updated`.
*Accept:* paging through 250 items created in the same millisecond returns each exactly once.

**REQ-KNO-13** MUST `[U3][D]` **Cataloguing: tag facets and discovery.** `kb_tags(prefix, limit)` lists tags in use among the
active/draft items the caller can see, with item counts, most used first then by name, optionally narrowed by a prefix such as
`domain/`; limit 1-500 (default 100). `kb_list(kinds, tags, status, mine_only, limit, cursor, sort)` browses without a query;
`tags` requires all listed tags; `status` is one of `current` (active+draft), `all`, `draft`, `active`, `superseded`,
`archived`; `mine_only` hides referenced items. The server instructions tell agents to look at `kb_tags` before inventing a tag
and to use facet-style tags (`domain/rf`, `kind/formula`, `project/x`).
*Accept:* `kb_tags prefix=domain/` returns only tags starting with `domain/`; counts exclude archived items and items the
caller cannot see.

**REQ-KNO-14** SHOULD `[U2][U25]` **Retagging and re-organisation on request.** Agents can retag in bulk (`kb_retag`), rename a tag
everywhere (`kb_rename_tag`, dry run by default), find similar items (`kb_similar`), and consolidate (`kb_merge`); the
agent writes the merged text, the server makes the change safely. The server has no summariser of its own (an optional model
may draft a proposal, REQ-LLM-01). See REQ-CUR-01..04.

**REQ-KNO-15** MUST `[D]` **Size of tool results.** Bodies are returned in full only by `kb_get` (at most 20,000 characters,
`truncated=true` beyond that) and by `kb_version`; search results return a snippet of at most 400 characters. Tool results
are compact on purpose.
*Accept:* a 30,000-character body returns 20,000 characters and `truncated=true` from `kb_get`.

**REQ-KNO-16** MUST `[D]` **Every write is sanitised.** Every code path that stores or changes title/body text (remember,
update, session reports, ingestion, merges, connectors, Slack and Teams saves, restore) goes through the same sanitiser:
hidden characters removed (REQ-ING-12), secrets masked (REQ-ING-13), prompt-injection heuristics flagged (REQ-ING-14).
*Accept:* `kb_update` with a body containing an AWS key stores the masked text; no write path bypasses it.

**REQ-KNO-17** MUST `[D]` **Time and clocks.** All timestamps are UTC; services take an injectable clock; stored timestamps
sort in time order regardless of the zone of the caller.
*Accept:* a test with a fixed clock reproduces freshness weighting; items created with non-UTC offsets order correctly.

### 3.2 Access control, roles and promotion (ACC)

**REQ-ACC-01** MUST `[U3][D]` **Tenant boundary.** An org is a hard tenant boundary. Every read and write first requires the
item's `org_id` to equal the caller's org; items of another org are never visible, never linkable, and never returned by any
index. Stored files are namespaced per org so that knowing a file hash gives no access across orgs.
*Accept:* with two orgs in one database, no tool, list, search, graph, digest or resource of org A ever returns an item,
edge, promotion, audit event or file of org B, including when identifiers are guessed.

**REQ-ACC-02** MUST `[D]` **Roles.** Roles are ordered `viewer < member < editor < admin`.
`viewer` reads only; `member` reads and writes its own knowledge; `editor` may additionally edit any item in scopes it belongs
to; `admin` is an editor plus approvals, admin tools and the admin pages. A missing role claim or an unknown role name means
`viewer`.
*Accept:* a token whose `hive_role` is `"superuser"` can read but not write.

**REQ-ACC-03** MUST `[D]` **One visibility rule.** A single function decides visibility for every store and tool. For a caller
and an item ACL (org, scope, visibility): if the org differs the item is invisible; if the item's scope is one of the caller's
member scopes it is `PRIMARY`; otherwise if visibility is `org` it is `REFERENCED`; otherwise invisible. A caller's member
scopes are: their own private scope, the org scope, each project and each team they belong to.
Storage adapters translate this rule into their query language only as a prefilter and always agree with the Python rule;
hits from indexes are candidates that are re-checked through the item repository.
*Accept:* a randomised property test shows the SQL access filter of each adapter agrees with the Python rule.

**REQ-ACC-04** MUST `[D]` **Write rules.** A caller may create an item only if their role is at least `member`, the org matches
and they are a member of the target scope. A caller may write (update, retag, status, relocate, link from) an existing item if
they can create at its scope and either the scope is `private` and they own it, or they authored it, or their role is at
least `editor`. Referenced items are read-only.
*Accept:* a `viewer` cannot create; a `member` cannot edit another member's project note; an `editor` can; nobody can edit a
referenced item.

**REQ-ACC-05** MUST `[D]` **Existence never leaks.** A missing item and an item the caller cannot see produce the same
not-found result ("not found, or not visible to you"). Permission errors are produced only for items the caller can read.
*Accept:* `kb_get` on a nonexistent id and on another user's private id return identical messages.

**REQ-ACC-06** MUST `[U4][D]` **Self-service sharing stops at project level.** A member may move their own item to a
project they belong to (`kb_share`, `kb_remember share_with_project`) with visibility `scope`. Sharing at team or org level, or
with org-wide visibility, needs an approver. Admins, and approvers of the target scope, may share directly there.
*Accept:* `kb_remember` into a team scope by a plain member fails with a message that points to `kb_request_promotion`;
the same call by an admin succeeds.

**REQ-ACC-07** MUST `[U4]` **Promotion needs an approver.** Wider sharing is a *promotion request*: `kb_request_promotion(id,
to, owner, org_wide, reason)` with `to` in `project|team|org`. The requester must be able to edit the item and belong to the
target scope; archived items cannot be promoted; the target must differ from where the item already is; a request that the
requester could instead do directly (`kb_share`) is refused with a message saying so. `reason` is at most 2,000 characters.
`org_wide=true` makes the item visible, labelled external, to other groups of the org. The request records the item's
revision. There is at most one pending request per item.
*Accept:* a second pending request for the same item fails; a request for an archived item fails; a request to a scope the
requester is not in fails.

**REQ-ACC-08** MUST `[U4][D]` **Who approves.** Admins approve any request in their org. A non-admin member (never a viewer)
can be named approver of specific scopes (`approves`, textual keys such as `team:t1`, `project:p1`); they see and decide only
requests whose target is one of those scopes. Org-level requests stay with admins unless an `org:<id>` scope is named.
Nobody can approve a private-scope move.
*Accept:* an approver of `team:t1` cannot decide a request into `team:t2`; a viewer named approver still cannot decide.

**REQ-ACC-09** MUST `[D]` **Decisions are exactly-once and not self-serving.** `kb_decide_promotion(id, decision, note)` with
decision `approve`, `reject` or `withdraw`. Approvers approve/reject; only the requester can withdraw. Nobody can decide their
own request, even if later made admin. Decisions are compare-and-set transitions from `pending`: of concurrent approvers
exactly one wins and the others get a conflict. Allowed transitions: `pending` to `approved|rejected|withdrawn|stale`; and
`approved` to `stale` only when the move itself failed. A `note` is at most 2,000 characters.
*Accept:* 10 concurrent approvals produce one `approved`; self-approval fails.

**REQ-ACC-10** MUST `[D]` **Reviewed version only.** An approval moves exactly the revision the approver was shown. If the
item was edited (or vanished) after the request, the request becomes `stale` and nothing is published; the requester must
ask again.
*Accept:* request, edit item, approve: the item is not moved, the request is `stale`, an error says to request again.

**REQ-ACC-11** MUST `[D]` **Crash-safe approval.** Before an approval is recorded, a durable repair job `apply_promotion` is
queued (delayed 60 seconds, de-duplicated per promotion). If the process dies before the item is moved, the job completes the
move; if the decision was never recorded or is already settled the job does nothing; if the item changed meanwhile the request
becomes `stale`.
*Accept:* killing the process between claim and move leaves no half-approved state after the job runs.

**REQ-ACC-12** MUST `[D]` **Indexes follow the move.** After any relocation both search indexes are updated with the new
access rules in the same operation (`sync_acl`), and the item is re-indexed when the move crosses the embedding privacy boundary
(REQ-EMB-05). Index entries never grant access on their own.
*Accept:* after approval, a member of the target scope finds the item by keyword and by vector; a non-member does not.

**REQ-ACC-13** MUST `[D]` **Approvers read what they review.** `kb_list_promotions` returns, for approvers and only for open
requests, the item title, body (at most 4,000 characters), scope and author, including for private items; this is audited.
Everyone else sees only their own requests. `status` filter: `pending` (default), `approved`, `rejected`, `withdrawn`, `stale`,
`all`; `mine=true` limits approvers to their own requests.
*Accept:* a requester's own list never contains `review_body`; an approver's pending list does.

**REQ-ACC-14** MUST `[U2][D]` **Identity comes only from the token.** No tool, route or CLI argument that acts as a user
accepts a user, org, role, team or project argument for the caller. Extra tool arguments are ignored. The `Principal` is built
only from verified claims: `sub` and `hive_org` (required, at most 200 characters, no NUL), `hive_role`, `hive_teams`,
`hive_projects`, `hive_approves` (lists of at most 1,000 strings each).
*Accept:* a token without an org claim is refused; claims with 1,001 projects are refused.

**REQ-ACC-15** MUST `[U3]` **Referenced knowledge from other groups, labelled.** Items that the caller may see only because they
are org-visible from a scope the caller does not belong to are returned separately (`from_other_groups`), with an origin line
`EXTERNAL: shared from <where> by <author> (not your group)`. The caller can adopt and extend them (by remembering a new note
linked `derived_from`), or ignore them; foreign text is never silently merged into the caller's own items.
*Accept:* every result and graph node carries `origin` and `external`; a note that builds on a foreign one is a separate item
with a `derived_from` edge.

**REQ-ACC-16** MUST `[D]` **Audit of access-relevant changes.** Item create, update, relocate, purge, archive-by-rule,
confirm, promotion request and every decision, edge add/remove, user forget, integration update, admin sign-in and every admin
user/token change write an audit event (Annex G.5) containing ids and short facts and never content.
*Accept:* after each of those operations the audit trail has one event with the actor and target.

**REQ-ACC-17** MUST `[D]` **Merge and import placement.** A merged or consolidated item stays in the sources' shared scope only
when all sources are in the same place and the caller may publish there directly; otherwise it is created private.
*Accept:* merging a project note with a private note yields a private result.

**REQ-ACC-18** MUST `[U45]` **Working with other groups' notes.** Six tools (Annex A.1): `kb_fork` copies a readable note into the caller's own scope (private by default, or a project they belong to) linked `derived_from`, with `source` `fork:<id>@<revision>` and an idempotency key so the same fork twice is one note; `kb_diff_upstream` shows a unified diff of the fork against the original now (at most 200 lines; the original gone or hidden is reported, not an error); `kb_ignore` hides one *external* note from the caller's own results only (at most 1,000 per person; hiding one's own or a project mate's note is refused; un-hiding needs no check); `kb_suggest_edit` leaves the full replacement title and/or body (secrets masked, same size rules as an edit, a note of at most 1,000 characters) for people who may edit the note, one open suggestion per person and note (a new one replaces it), at most 20 open per person, kept 180 days; `kb_list_suggestions` shows open (optionally decided) suggestions only for notes the caller may edit, newest first, at most 50; `kb_decide_suggestion` accepts (applies the text with the same revision check as an approval: refused if the note moved on since) or rejects, and a decided record is kept 30 days. A person who can edit a note is told to use `kb_update` instead of suggesting. Forgetting a person deletes their hidden list and open suggestions; with `--erase-audit` the decided ones are renamed to the alias. Audit actions: `item.fork`, `item.ignore`, `item.unignore`, `suggestion.create/accept/reject`.
*Accept:* a hidden external note stays out of the hider's results and remains in everyone else's; accepting a suggestion made against revision 2 after a third revision is refused.

### 3.3 Search and ranking (SRCH)

**REQ-SRCH-01** MUST `[U2][U3][D]` **Hybrid retrieval.** `kb_consult(query, limit, kinds, tags, other_groups, include_links,
session)` runs keyword and semantic search together, fuses them, re-checks access through the repository, weights, ranks and
returns compact results with snippets. `limit` is 1-25 (default 8). Candidates are pulled from each index with a pool of
`max(limit x 5, 40)`.
*Accept:* a query whose words do not appear in a note but whose meaning matches finds it (with a real embedder); a query with
an exact identifier finds the note through the keyword list.

**REQ-SRCH-02** MUST `[D]` **Keyword index semantics.** The keyword index is Unicode-aware and case-insensitive for every
script (searching "радар" finds "Радар"; ASCII-only `LIKE` is not acceptable). Text is tokenised by one shared tokenizer
(NFKC, case-fold, words are runs of letters/digits) and indexed as title, body and tags together. Query terms are the query's
words minus a fixed English stopword list (if only stopwords remain, all words are used); terms are OR-ed; items matching more
and rarer terms rank higher; one hit per item. Hostile query text can never raise an error or inject syntax (terms are always
quoted for FTS5 / tsquery).
*Accept:* a query of "the for" still searches; a query containing `" OR * ) ( -` returns results or nothing but never an error.

**REQ-SRCH-03** MUST `[D]` **Vector search semantics.** Vector search is cosine similarity on L2-normalised vectors, scoped to
one embedding model (vectors of different models are never compared), returns one hit per item (its best chunk), and applies the
caller's access filter before choosing neighbours. Vector hits below the embedder's relevance floor (`min_similarity`) are
dropped. The vector query is skipped when the query has no word.
*Accept:* with the hashing embedder, an unrelated query returns no vector hits; the filter is applied before the nearest-neighbour
cut, so a caller who may see 0.3% of the data still gets full recall (REQ-NFR-PERF-04).

**REQ-SRCH-04** MUST `[D]` **Fusion.** Keyword and vector rankings are fused by weighted reciprocal rank fusion with `k=60`:
the keyword list contributes `1/(60+rank)` for its first 5 hits only (`keyword_depth=5`); the vector list contributes
`2/(60+rank)` (`vector_weight=2.0`). Repeats within a list count once. Optional, off by default
(`keyword_lead_ratio=0`, setting `HIVE_KEYWORD_LEAD_RATIO`): when the best keyword hit scores at least that many times the
second best (and there are two or more hits) and the vector list does not contain it, its fused score is raised just above the
best one, so an exact id or error code that only the keyword index can find is ranked first. `hive eval` honours the setting.
*Accept:* unit tests on `rrf` and `fuse` with these constants, and on the lead (lifted only when clearly ahead, not when the
vector list has it, not when it is a lone hit, never when off); on `evals/core.json` hybrid R@3 is at least 0.95 with the bge model.

**REQ-SRCH-05** MUST `[D]` **The repository is the authority.** Every hit is re-read through the item repository with the
caller's identity (`get_many`); a stale or wrong index can never reveal an item. Invisible and missing ids are dropped.
*Accept:* poisoning the text index with an id the caller cannot see yields nothing for that id.

**REQ-SRCH-06** MUST `[U3][D]` **Relevance weighting.** The fused score is multiplied by
`scope_weight x referenced_factor x status_weight x (0.5 + 0.5 x freshness)` where `scope_weight` is
private 1.0, project 1.0, team 0.85, org 0.7; `referenced_factor` is 0.6 for referenced items and 1.0 otherwise;
`status_weight` is active 1.0, draft 0.8, superseded 0.4, archived 0.0; `freshness = 0.5 ^ (age_days / half_life)` with half-lives
session 30 d, compaction 45 d, note 180 d, research/measurement/decision 365 d, and no ageing (1.0) for formula, best_practice,
doc, howto and entity. Clock skew (negative age) never boosts. Ties are broken by item id.
*Accept:* an old session log ranks below an equally relevant fresh one; formulas do not age.

**REQ-SRCH-07** MUST `[U3]` **Other groups' knowledge.** `other_groups` is `auto` (default), `always` or `never`. In `auto` mode
referenced results are added only when the caller's own scopes have fewer than 3 matching results (`min_primary=3`), and then
`max(limit - shown, 2)` of them; `always` adds up to `limit`; `never` adds none. The response has separate lists `results` and
`from_other_groups`, and a note explaining why external results appeared.
*Accept:* with 5 own matches `auto` returns no external results; with 1 own match it returns external results labelled EXTERNAL.

**REQ-SRCH-08** MUST `[D]` **Filters.** `kinds` restricts to the named kinds; `tags` requires all named tags (normalised);
only `active` and `draft` items are returned by default; superseded and archived items are excluded unless the service is called
with a wider status set.
*Accept:* `kinds=["formula"]` returns only formulas; two tags require both.

**REQ-SRCH-09** MUST `[D]` **Snippets.** A result carries a snippet (at most 400 characters): the body chunk (120 words, 20
overlap) with the most distinct query words; ties go to the chunk the vector search found, then the first chunk. The snippet never
depends on a vector hit being present.
*Accept:* a query matching only the third paragraph of a long note shows that paragraph.

**REQ-SRCH-10** MUST `[D]` **Embedding outage degrades gracefully.** If the embedder is unavailable the consult returns keyword
results and a note "semantic search unavailable; showing keyword matches only"; if the item write succeeded the indexing job
retries later (REQ-EMB-11).
*Accept:* a failing embedder makes `kb_consult` succeed with the note and keyword hits.

**REQ-SRCH-11** MAY `[U27][D]` **Cross-encoder rerank (off by default).** With `HIVE_RERANKER=onnx` the best
`max(HIVE_RERANK_DEPTH (default 15), 2 x limit)` candidates are rescored by a cross-encoder reading query and `title + body[:1200]`;
results are re-ordered by the score (sigmoid of the logit); candidates scoring below the model's `min_score` are dropped
(shipped model: -100, i.e. none); on failure the first-stage order is kept with a note. It is English-only.
*Accept:* with a fake reranker the output order follows its scores; with it failing the first-stage order is returned with a note.

**REQ-SRCH-12** SHOULD `[U2][U27][D]` **Evaluation harness.** `hive eval <set.json> [--sweep] [--floor f] [--recommend]` indexes a labelled
set (`docs:[{id,title,body}]`, `queries:[{q, relevant:[ids], type}]`) in memory with the configured embedder and reports
recall@1/3/10, MRR, the rate of queries that found nothing, and the false-positive rate on negative queries; `--recommend`
sweeps the relevance floor in steps of 0.01 and prints `Suggested: HIVE_RELEVANCE_FLOOR=0.xx`, the floor with the best recall@3
among those answering at most 5% of unrelated queries. Shipped sets: `evals/core.json` (48 documents, 96 positive and 12 negative
queries), `evals/multilingual.json` (14 documents, 34 queries incl. 6 negative), `evals/conflicts.json`, `evals/merges.json`.
*Accept:* running `hive eval evals/core.json` with the hashing embedder completes and prints the metrics table.

**REQ-SRCH-13** MUST `[U27][D]` **Graph-aware answers.** Unless `include_links=false`, each result carries up to 5 links to
other items the caller can see (not archived), ordered by relation importance (supersedes, contradicts, derived_from, supports,
part_of, next, then others), and the response adds up to 5 deduplicated notes for two situations: a result that a newer visible
item supersedes ("has been superseded by ... (not among these results; read it first)") and results that contradict each other
("... contradict each other: reconcile them before relying on either"). Edge lookups are bounded (200 edges per call).
*Accept:* linking B `supersedes` A then consulting a query that finds A yields the superseded note naming B.

**REQ-SRCH-14** SHOULD `[U29][U34][D]` **Search feedback, opt-in.** With `HIVE_SEARCH_FEEDBACK=true`, `kb_consult` records the search text
(secrets masked, at most 200 characters) and up to 25 returned ids (`search.query`), `kb_get` records opens (`search.open`) and
`kb_rate(id, useful, query)` records an explicit verdict (`search.rating`), all in the audit trail readable by admins only.
`kb_rate` only works on items the caller can see, and reports `recorded=false` when the setting is off. The server instructions tell
agents to rate results after `kb_consult` only when the setting is on. `hive eval-build <out> --org O [--days 30] [--distractors 50]
[--negatives file]` builds an evaluation file from the trail: a search followed within 30 minutes by opening a result labels that
result relevant to that search (a "useful" rating credits the search that showed it; "not useful" removes it even if opened);
the file contains real item text and is written with mode 0600. `hive feedback-report --org O [--days 30]` lists searches that found
nothing, searches whose results nobody used, the most useful notes and the ones rated not useful.
*Accept:* with the setting off nothing is recorded; with it on a consult-then-get sequence yields one labelled pair in `eval-build`.

**REQ-SRCH-15** MAY `[U36][U42][D]` **Confirmation nudge (off by default).** With `HIVE_RANK_SIGNALS=true` an item confirmed with
`kb_confirm` within the last 90 days is ranked 2% higher (multiplier 1.02). The nudge breaks near-ties and cannot lift a clearly worse
match. Votes from `kb_rate` do **not** influence ranking (decided with the user, ledger U42: they showed no gain in a simulated replay);
they remain feedback data only.
*Accept:* with the flag on, two equal items rank the confirmed one first; a `kb_rate` vote changes no ranking.

**REQ-SRCH-16** MUST `[D]` **Result shape.** A result contains: id, title, snippet, kind, tags, status, score (rounded to 5
decimals), origin, external, scope (`level:owner`), author, updated_at (ISO), revision, links. See Annex A.2.
*Accept:* the contract test compares the generated tool schema with Annex A.

### 3.4 Embeddings (EMB)

**REQ-EMB-01** MUST `[U3]` **Pluggable embedders; offline and hosted both supported.** An embedder has `model_id`, `dim`,
`min_similarity`, `embed(texts)` and `embed_query(text)`. Three kinds are selected by `HIVE_EMBEDDER`: `hashing` (development and
tests only), `onnx` (offline) and `openai` (any OpenAI-compatible hosted `/embeddings`, including Azure OpenAI).
Every embedder returns one finite, non-zero vector per text, in order, and refuses wordless text with a validation error.
*Accept:* the same conformance tests pass against all three (hosted tested against a fake server).

**REQ-EMB-02** MUST `[D]` **Hashing embedder.** Feature hashing over the shared tokenizer into 256 dimensions (blake2b, signed
buckets; if signed collisions cancel to zero, unsigned counts), `model_id = hashing-256`, `min_similarity = 0.25`. It captures
word overlap only.

**REQ-EMB-03** MUST `[U3][D]` **Local ONNX models, pinned.** Two models are pinned by URL and SHA-256 (Annex H.2):
`bge-small-en-v1.5-int8` (default; 384 d; CLS pooling; query prefix "Represent this sentence for searching relevant passages: ";
max 256 tokens; floor 0.58) and `multilingual-e5-small-int8` (384 d; mean pooling; prefixes `query: ` and `passage: `; floor 0.81;
about 100 languages). The model id includes name, pooling and max tokens so that any change to the vectors changes the id.
Vectors are L2-normalised; texts are processed in batches of 32 off the event loop; emptiness is validated before the prefix is added.
`hive models fetch [name]` downloads the files, verifies SHA-256 and swaps them in atomically; **nothing is downloaded when the
server starts**. A file with the wrong hash is replaced. The image may bake models in at build time.
*Accept:* with the real model, relevant pairs score at least the floor and unrelated pairs below it on the shipped evaluation sets.

**REQ-EMB-04** MUST `[U3][D]` **Hosted OpenAI-compatible embedder.** Configured by URL, API key (bearer, or `api-key` header for
Azure), model, requested dimensions (`HIVE_EMBEDDING_DIM`, default 1024, 8-4096), relevance floor (`HIVE_EMBEDDING_MIN_SIMILARITY`,
default 0.3). It batches 64 texts, retries transport errors and HTTP 408/429/500/502/503/504 up to 3 times with exponential backoff
(0.5 s base), rejects other HTTP errors immediately, and maps every failure and malformed answer (wrong count, wrong size,
non-numeric, zero or non-finite vector) to "embedding unavailable". `model_id = openai-<model>-<dim>`.
*Accept:* against a fake server: retry on 503 succeeds; a vector of the wrong size is refused.

**REQ-EMB-05** MUST `[U3][D]` **Privacy rule for hosted embedders.** Item text narrower than `HIVE_EMBEDDING_MIN_SCOPE` is never
sent to the embedder. The default is `private` (everything) for local embedders and `project` for hosted ones, so private notes and
session logs stay keyword-only with a hosted provider and are embedded the moment they are promoted. The same policy applies to
`kb_similar`. Search *queries* are always sent to a hosted provider; this is documented.
*Accept:* with the `openai` embedder and a fake server, a private note causes no outgoing embedding request; sharing it to a project
causes one.

**REQ-EMB-06** MUST `[D]` **Vectors are per model.** Vectors are stored per `model_id`; the vector index replaces *all* chunks of
an item for a model atomically (an empty list removes them); bad vectors (zero, NaN, wrong dimension relative to the model's first
vector) leave the previous state untouched; deleting an item removes its vectors for every model.
*Accept:* conformance tests on all backends.

**REQ-EMB-07** MUST `[U27][D]` **Switching models safely.** `hive reindex --org <id>` queues one `index_item` job per item
(de-duplicated per item, safe to repeat); the job runner re-embeds without restarting the server. `hive vectors status` shows, per model,
the number of items with vectors and the current model's coverage; `hive vectors prune --yes` drops other models' vectors and refuses
while the current model covers less than 95% of non-archived items unless `--force`. Metrics expose coverage (REQ-OBS-03).
*Accept:* after switching `HIVE_EMBEDDER`, consult still works by keyword; after reindex coverage reaches 1.0; prune refuses at 0.5.

**REQ-EMB-08** MUST `[D]` **Chunking and what is embedded.** A body is split into chunks of at most 120 words with 20 words of
overlap, packing paragraphs (blank-line separated) and splitting over-long paragraphs; every word lands in at least one chunk; the
text embedded for a chunk is `title + "\n\n" + chunk`; chunk ordinal equals its position. Wordless chunks reuse another text so
ordinals stay aligned; an item with no words anywhere has no vectors (found by id only).
*Accept:* chunking is deterministic and never returns an empty chunk; every input word appears in some chunk.

**REQ-EMB-09** MUST `[D]` **Indexing never loses an item.** Creating or updating an item writes it first, then indexes it; the
keyword index is written before vectors; if indexing fails with any expected error, a durable job `index_item` (dedupe key
`index:<id>`) is queued and the tool reports `indexed=false` with a message that indexing will complete shortly. Index jobs read the
latest stored version and repeat up to 3 rounds if the item changes meanwhile; an item missing from its org is ignored (nothing is
deleted by id across orgs). Archived items are removed from both indexes.
*Accept:* with a failing embedder `kb_remember` returns `indexed=false`, the item is retrievable by id and keyword, and after the
embedder recovers the job indexes it.

**REQ-EMB-10** SHOULD `[U29][D]` **Relevance floors are overridable.** `HIVE_RELEVANCE_FLOOR` (0-1) overrides the active embedder's
floor; `hive eval --recommend` suggests a value (REQ-SRCH-12).

**REQ-EMB-11** MAY `[U29][U32][D]` **Optional LanceDB vector store.** `HIVE_VECTOR_STORE=lancedb` (embedded profile only, optional
extra) keeps vectors in a LanceDB directory (`<data>/lance`, one table per model named `vec_<sha256(model_id)[:24]>`), passes the same
vector conformance suite, applies the access filter *before* nearest-neighbour selection (with the index too), escapes scope names
in its filter, compacts every 100 writes, and builds an IVF_FLAT index automatically at 50,000 rows (rebuilt at double that size,
probing 85% of `sqrt(n)` partitions). Vectors are derived data: after switching stores or restoring run `hive reindex`; backups do
not include it. It is documented as worthwhile only above roughly 100,000 vectors.
*Accept:* hostile scope names cannot change the filter; the vector conformance suite passes.

### 3.5 Sessions, agent integration and client installation (SESS)

**REQ-SESS-01** MUST `[U2]` **Session reports.** `kb_report_session(summary, log, outcomes, project, idempotency_key, session)` stores
a *session* item: title `Session: <first non-empty summary line, at most 120 characters>`, body = the summary plus, when outcomes are
given, a blank line, `Outcomes:` and one `- <outcome>` line each; tags `kind/session` and, when `project` is given, `project/<name>`.
The item is private to the caller (the `project` argument only tags it).
*Accept:* the report is searchable by its summary; another user cannot see it.

**REQ-SESS-02** MUST `[U2]` **Compaction reports.** `kb_report_compaction(summary, log, outcomes, project, idempotency_key)` is the
same with kind `compaction`, title `Compaction: ...`, tag `kind/compaction`, so a context summary can be recovered after the agent's
context is compacted.

**REQ-SESS-03** MUST `[U2][D]` **Full logs as stored files.** The optional `log` is redacted (REQ-ING-13), at most 5 MiB, and stored in
the blob store, content-addressed per org; the item's `source` becomes `blob:<sha256>:<size>`. Redaction counts from the log and the
summary are merged in the response. Larger logs are refused.
*Accept:* a log with a private key stores `[REDACTED:private-key]`; a 6 MiB log is refused.

**REQ-SESS-04** MUST `[D]` **Reading a log.** `kb_get_log(id)` returns the stored log of a report the caller can read; access is checked
through the item; an item without a `blob:` source gives not-found.

**REQ-SESS-05** SHOULD `[U34][D]` **"What did the agent use?" trails.** An agent may pass one label (4-80 characters of letters, digits and
`. _ : -`) as `session` to `kb_consult`, `kb_get` and `kb_report_session`. For one day (24 h TTL) and per (org, person, label) the server
remembers ids only: the notes shown by searches and the notes opened (at most 100 each). `kb_session_sources(session)` lists them, opened
first, with origin, author and last-change time, re-checking access on read (a note that became private is not listed). `kb_report_session`
with the label links the stored session note `derived_from` up to 20 opened notes, so the trail survives in the graph. A label belongs
to the person who used it; nobody else can read it.
*Accept:* after consult+get with a label, `kb_session_sources` lists the opened note first; a second user with the same label sees nothing.

**REQ-SESS-06** MUST `[U2][U29][D]` **Claude Code hooks.** The client installs four hooks into Claude Code's settings:
`SessionStart` (timeout 10 s): says the knowledge base exists and lists the top 5 results of `"<project> project"` (project = the working
directory's name); `UserPromptSubmit` (8 s): for a prompt of at least 6 words that does not start with `/`, searches (query = first 500
characters, limit 3) and adds up to 3 relevant results, staying silent when nothing is found; `PreCompact` (30 s): stores the conversation
through `kb_report_compaction`; `SessionEnd` (30 s): stores it through `kb_report_session`. Added context is capped at 3,000 characters and
is output as Claude Code's JSON (`hookSpecificOutput` with `hookEventName` and `additionalContext`). Each hook passes the session label
`cc-<session id>` (sanitised, at most 80 characters) so trails work automatically.
*Accept:* against a live server, each hook produces the described effect; a prompt of 5 words causes no search.

**REQ-SESS-07** MUST `[U29][D]` **Hooks fail open.** Hooks have short deadlines (calls 5 s; uploads at most 15 s inside a 28 s budget), never
print errors into the session, never raise, and do nothing when `HIVE_URL` or `HIVE_TOKEN` (or the file `~/.config/hive/token`) is missing or
the server is down. `HIVE_DEBUG=1` prints the reason a hook stayed silent to stderr.
*Accept:* with no server running every hook exits 0 and prints nothing.

**REQ-SESS-08** MUST `[D]` **Hook uploads are idempotent.** The report idempotency key is `hook-<tool>-<session id>-<number of turns>`
(at most 120 characters), so the same session content is never stored twice.

**REQ-SESS-09** SHOULD `[U32][D]` **Summaries without a model in the loop.** The default summary is extractive: first request (300
characters), up to five of the agent's most informative sentences (scored by signal words, digits, inline code, lateness; filler and
code skipped; 25-300 characters; in chronological order; 700-character budget), then the commit messages (up to 5) and the files edited
(first 4 named). The summary is at most 1,500 characters. If `HIVE_HOOK_SUMMARIZER` names a command it is run directly (no shell) with
the last 30,000 characters of the conversation on stdin and its output (at most 2,000 characters) is used; the timeout is
`HIVE_HOOK_SUMMARIZER_TIMEOUT` (default 20 s, maximum 25 s); on failure, empty output or timeout the process is killed and the
extractive summary is used. The transcript is read defensively (tail of at most 4 MiB, unreadable lines skipped, rendered log at most
60,000 characters).
*Accept:* a slow summarizer is killed and the extractive summary is stored; commit messages written with `git commit -am` are found.

**REQ-SESS-10** MUST `[U29][D]` **Installing hooks.** `hive-hooks hooks install [--scope user|project|local] [--command C] [--no-prompt-hook]
[--dry-run]` merges the hook entries (`<command> hook <event>`) into `~/.claude/settings.json` (user), `.claude/settings.json` (project) or
`.claude/settings.local.json` (local). It keeps other hooks and settings, replaces older copies of its own entries, is idempotent, keeps the
original once as `<file>.hive-backup`, writes atomically preserving file mode, refuses a file that is not a JSON object (nothing changed),
and `hooks remove` removes exactly its own entries. `hooks status` reports where hooks are installed and whether the server answers.
*Accept:* running install twice yields identical files; a foreign hook whose command merely resembles ours is not removed.

**REQ-SESS-11** MUST `[U29][D]` **Instructions note for other agents.** `hooks snippet [--file F] [--remove]` writes (or removes) an
instruction note between `<!-- hive-mcp:begin -->` and `<!-- hive-mcp:end -->` in `AGENTS.md`, `CLAUDE.md` or `GEMINI.md`; running it again
updates it in place; the note tells agents to consult before a task, remember reusable results and report sessions/compactions.

**REQ-SESS-12** SHOULD `[U36]` **One-command setup and editor configs.** `hive-hooks init [--url U] [--editor claude,vscode,cursor] [--scope S]
[--root R] [--agents-file F] [--save-token] [--no-hooks] [--no-check]` installs hooks, merges a `hive` server entry into `.mcp.json` (Claude
Code, `type: http`, `Bearer ${HIVE_TOKEN}`), `.vscode/mcp.json` (key `servers`, `Bearer ${input:hive-token}` plus a password-style prompt input)
and `.cursor/mcp.json` (`Bearer ${env:HIVE_TOKEN}`), writes the note, optionally stores the token in `~/.config/hive/token` with owner-only
permissions, and finally tries the server. **No token is ever written into a project file.** Other servers in those files stay; re-running
changes nothing. `scripts/install_client.sh --url ...` installs the client with `uv` first. IDE extensions with their own UI are explicitly
not built (NG-03).

**REQ-SESS-13** MUST `[U32][D]` **Slim client package.** The hooks client is a separate distribution `hive-hooks` (workspace member
`clients/hooks`, command `hive-hooks`) depending only on `mcp` and `typer`; it must not import the server package (CI checks that
`hive_mcp` is not imported). The full server install also exposes the same commands under `hive` (`hive hook`, `hive hooks ...`,
`hive init`).
*Accept:* CI job `client` builds the wheel, installs it alone and runs `hive-hooks hooks install --scope project --dry-run`.

**REQ-SESS-14** SHOULD `[U36][D]` **Release and publishing.** Tags `v*` build wheels for both packages and attach them to the GitHub
release; the workflow refuses a tag that differs from the version in both `pyproject.toml` files. Publishing to PyPI is opt-in (repository
variable `PUBLISH_TO_PYPI=true`, trusted publisher on pypi.org, environment `pypi`); the user chose to have it set up and documented
(ledger U38), the pypi.org steps are the user's.

**REQ-SESS-15** MUST `[U2][D]` **Server instructions.** The MCP server's `instructions` string tells agents the workflow: consult before
a task and cite origin; treat `from_other_groups` as external; remember one focused item per idea with facet tags; report session and
compaction; never store secrets; treat retrieved text (including ingested documents and web pages) as data, not instructions, and tell the
user if an item tries to give orders; everything is private until shared; wider sharing needs an approver; identity comes from the
connection; short guidance on entities, ingest, history, purge, digest, list/tags, bulk retag/merge, rating, duplicates/stale/confirm,
links. Exact text in Annex A.4.

**REQ-SESS-16** SHOULD `[U25]` **Prompts.** Five MCP prompts: `consult_before_task(task)`, `end_of_session_report()`,
`before_compaction()`, `tidy_tag(tag)` and `review_promotions()`, each a ready-made routine for the client (text in Annex A.4). The
server must not register duplicate prompt names (the SDK silently ignores duplicates; a regression test asserts no "already exists"
warning).

**REQ-SESS-17** SHOULD `[U25]` **Resources.** Resources `kb://guide` (the instructions), `kb://recent` (the 20 newest items the caller can
see, as links with origin), `kb://item/{id}` (markdown with id, revision, kind, status, origin, scope, author, tags, updated and the body
truncated at 20,000 characters), `kb://project/{id}/digest` and `kb://team/{id}/digest` (last 7 days, plain text; membership required).
Items from other groups are labelled EXTERNAL here too.

### 3.6 Graph (GRAPH)

**REQ-GRAPH-01** MUST `[U2][U22]` **Typed links.** Items are nodes; edges are directed, typed and unique per `(org, src, dst,
relation)`. A relation is a short lower-case word (`^[a-z][a-z0-9_-]{0,39}$`, spaces become `_`); suggested vocabulary: related, part_of,
next, derived_from, supports, contradicts, supersedes, mentions; any other conforming word is accepted. An edge needs two different items.
*Accept:* `kb_link a b "Part Of"` creates relation `part_of`; linking an item to itself is refused.

**REQ-GRAPH-02** MUST `[U3][D]` **Creating a link.** `kb_link(from_id, to_id, relation)` needs write access to the source and read access to
the target (which may be another group's item), is idempotent ("Already linked"), and is audited (`edge.add`).
*Accept:* a member can link their note `derived_from` another group's org-visible note.

**REQ-GRAPH-03** MUST `[D]` **A link never reveals.** An edge is returned only to a caller who can see both ends; traversal walks only
through items the caller can see; archived items are hidden from the graph. Moving an item changes who sees its links automatically.
*Accept:* after the target becomes private to someone else, the source's owner no longer sees the edge.

**REQ-GRAPH-04** MUST `[D]` **Traversal.** `kb_graph(id, depth 1-3, relations, max_nodes 1-100 (default 30))` returns nodes (start item
first, with depth, kind, scope, origin, external) and edges; `truncated=true` when more exist than returned (or an edge page of 200 was
full). A relation filter limits traversal to those relations.
*Accept:* depth 4 or max_nodes 101 is refused.

**REQ-GRAPH-05** MUST `[D]` **Removing links.** `kb_unlink` removes a link the caller created or one from an item they may edit;
others get not-found (existence of the link must not leak) or a permission message when they can see the source. Audited (`edge.remove`).

**REQ-GRAPH-06** MUST `[U22]` **Document structure as links.** Ingested parts are linked `part_of` the first part and chained `next`
(REQ-ING-03); `kb_merge` links `supersedes`; the session trail links `derived_from`; entity mentions link `mentions`.

### 3.7 Entities (ENT)

**REQ-ENT-01** SHOULD `[U27]` **Entities are items.** An entity (person, system, part, concept, customer...) is an ordinary item of kind
`entity` with a scope, visibility, history and links like any other. Its name and every alias are stored as tags `alias/<slug>` and its type
as `entity/<slug>`.

**REQ-ENT-02** SHOULD `[D]` **Name comparison.** `slug(name)` = NFKC, case-fold, `+`->` plus `, `#`->` sharp `, `&`->` and `, `@`->` at `,
every run of non-word characters (and `_`) to `-`, trimmed, at most 58 characters. So "Pulse-Doppler  radar", "pulse doppler radar" and
"PULSE_DOPPLER RADAR" are one name, while `C++`, `C#` and `C` stay different. A name with no usable character is refused.

**REQ-ENT-03** SHOULD `[U27]` **Resolve.** `kb_entity_resolve(name, type?)` returns the caller's entities known under that name or alias
(exact), with other groups' entities marked external; if none, close spellings (similarity ratio at least 0.85 against any alias, via the keyword
index candidates).

**REQ-ENT-04** SHOULD `[U27]` **Save.** `kb_entity_save(name, type, description?, aliases?, share_with_project?)` creates the entity or, if
the *name* already belongs to an entity the caller can edit (same type when given), adds new aliases and the description to it. At most 20
aliases. Only the name decides "same entity": an alias owned by another entity is reported in `conflicts` and is not stolen; if the *name*
itself belongs to an entity the caller cannot edit, saving is refused with advice to link instead. The response lists `similar` near-misses (up
to 3). To merge two entities use `kb_merge`.
*Accept:* saving "Radar-X" with alias "RX" then "radar x" with alias "RX2" yields one entity with both aliases.

**REQ-ENT-05** SHOULD `[U27]` **Mentions.** `kb_entity_mention(id, entities)` links a note the caller may edit to entities (ids or names) with
`mentions`; the result says per input `linked`, `already linked` or why not. `kb_graph(entity, relations=["mentions"])` lists the notes that
mention it.

**REQ-ENT-06** SHOULD `[U27]` **Mentions found at ingestion.** `kb_ingest` (unless `link_entities=false`) links each stored part to the caller's
**own** entities whose name or alias (at least 3 characters) occurs in the part (word n-grams up to 4; at most 2,000 entities scanned, at most
20 links per part). Entity discovery is not automatic (the server has no language model; agents propose entities).

### 3.8 Item history (HIST)

**REQ-HIST-01** MUST `[U27]` **Versions are kept in the same transaction.** Every edit and move (`kb_update`, `kb_share`, promotions, tag
changes, restores) stores the version it replaces (revision, title, body, kind, status, tags, scope, visibility, item author, written-at,
replaced-at, replaced-by) inside the same database transaction as the change. The newest 50 previous versions of each item are kept; older
ones are dropped.
*Accept:* after 60 edits `kb_history` shows 50 previous versions plus the current one (limit 1-51).

**REQ-HIST-02** MUST `[U27][D]` **Privacy of history.** A version can be read only by someone who could have read the item *as it was at that
revision* (its ACL at that time). A draft written while an item was private stays private to its author after sharing; if an item becomes
private again nobody else sees any of its history.
*Accept:* a project member sees history only from the moment of sharing; after demotion to private they see none.

**REQ-HIST-03** MUST `[U27]` **Tools.** `kb_history(id, limit)` (newest first: revision, title, written-at, replaced-by, replaced-at, scope),
`kb_version(id, revision)` (one version in full, truncated at 20,000 characters), `kb_diff(id, from_revision, to_revision?)` (unified diff of
`# title` and body, at most 20,000 characters, "(no difference)" when equal), `kb_restore(id, revision, expected_revision)` (makes the item say
what that version said as a *new* revision; refused when the item already matches; needs edit rights and a readable version).

**REQ-HIST-04** MUST `[D]` **Search indexes only the current version;** purging an item deletes its history (REQ-RET-02).

### 3.9 Ingestion and content screening (ING)

**REQ-ING-01** MUST `[U2][U22]` **Files become searchable, linked knowledge.** `kb_ingest(name, content, encoding, tags, share_with_project,
link_entities)` (text, or base64 for binary; at most 10 MiB through the tool) and `hive ingest PATH... --org O --user U [--role R] [--project P]
[--tag T]` (directories recursively; at most 50 MiB per file) add documents as items of kind `doc`. Private unless a project is given and the
user belongs to it. `name` picks the reader by extension. Items get tags `file/<slug of name>` and `format/<format>` plus the caller's tags, and
`source=file:<name>` (the original file is not kept).
*Accept:* ingesting a markdown file yields items titled from its first heading or name; a refused file stores nothing.

**REQ-ING-02** MUST `[U27]` **Formats.** Plain text and source code (extensions `.txt .md .markdown .rst .csv .tsv .json .yaml .yml .toml .ini
.log .py .js .ts .go .rs .java .c .h .cpp .sh .sql .xml .tex` and no extension), HTML (`.html .htm`; scripts, styles, noscript and template dropped,
headings and block structure kept, title read separately), Word `.docx` (headings, paragraphs, tables), Excel `.xlsx` (every sheet in workbook order,
rows as `a | b | c`, at most 20,000 rows per sheet and 16,384 columns), PowerPoint `.pptx` (slides, titles, speaker notes) and PDF (extra `documents`;
encrypted PDFs refused; at most 2,000 pages; page text joined by blank lines). Text decoding: UTF-16 with BOM, else UTF-8 (with BOM), else Latin-1;
a file with NUL bytes in its first 8 KB (other than UTF-16) is refused as binary; CRLF becomes LF. Old binary `.doc .xls .ppt` and unknown types
are refused with a message (suggesting saving as the `x` format); a file with no readable text is refused; NULs in extracted text are removed.
*Accept:* each format round-trips through a fixture; legacy formats give the stated refusal.

**REQ-ING-03** MUST `[U22][D]` **Splitting.** Extracted text longer than 40,000 characters is split at markdown headings (levels 1-3) then
paragraph, line and word boundaries into parts of at most 40,000 characters; small neighbouring parts are merged; each part becomes an item titled
`<base> (i/n)` (+ ` - <heading>`); the base is the document title or the file name. At most 200 parts per document. Part `i` is linked `part_of`
part 1 and part `i-1` is linked `next` to part `i`. Every non-blank character lands in exactly one part.
*Accept:* a 500 KB document yields about a dozen parts with `next` and `part_of` edges.

**REQ-ING-04** MUST `[D]` **Idempotent ingestion.** Each part is stored with idempotency key `ingest:<fingerprint>:<index>` where the fingerprint covers
name, scope, tags, kind and the bytes; ingesting the same bytes with the same name, tags and scope stores nothing new, so a failed bulk run can be
repeated. Changing any of them stores new items (old ones are kept; archive them with `kb_update`).

**REQ-ING-05** MUST `[D]` **Untrusted parsers are isolated.** PDF, Office and HTML parsing runs in a worker thread (never on the event loop). Office archives
are read with the standard library only: at most 5,000 members, 16 MiB per member, 64 MiB unpacked, DTDs and entities refused, 10,000,000 characters of
output; damaged or hostile files become a validation error, never a crash (fuzzed with random bytes, malformed containers, a zip bomb and 100,000-level nesting
within time limits).

**REQ-ING-06** MAY `[U27][D]` **URL ingestion (off by default).** With `HIVE_INGEST_URLS_ENABLED=true`, `kb_ingest_url(url, tags, share_with_project)` fetches
a page or document (html, pdf, text, Office, and with OCR images). For every hop of every redirect (at most 5): http(s) only; no credentials in the URL; the host
name is resolved by the server and **every** resulting address must be public (private, loopback, link-local such as the cloud metadata address, CGNAT, multicast
and site-local ranges refused; IPv4 hidden in IPv4-mapped, IPv4-compatible, NAT64 `64:ff9b::/96` and 6to4 `2002::/16` addresses is checked too) unless
`HIVE_INGEST_URL_ALLOW_PRIVATE=true`; the connection goes to the checked address (the DNS answer cannot change between check and connect) while TLS still verifies
the real host name (SNI); an optional host allow-list `HIVE_INGEST_URL_HOSTS` (subdomains included); the client ignores proxy environment variables; at most 20 MiB
streamed; 15 s per network operation and a 60 s overall deadline; only document content types (images only with OCR). The item records `source=url:<final address>`.
*Accept:* `http://169.254.169.254/`, `http://[::ffff:10.0.0.1]/`, a name resolving to 127.0.0.1 and a redirect to a private address are all refused.

**REQ-ING-07** MAY `[U27]` **Git ingestion (operator only).** `hive ingest-git URL --org O --user U [--ref R] [--project P] [--tag T] [--exclude GLOB]` shallow-clones
(`--depth 1`, no tags, single branch, no submodules, `protocol.ext.allow=never`, `--` before the URL, address may not start with `-`), reads documents, markdown,
code and Office files under 1 MiB, skips symlinks, hidden paths, binaries and default excludes (`node_modules/*, vendor/*, dist/*, build/*, target/*, __pycache__/*,
*.lock, *.min.js, *.min.css, package-lock.json, *.svg, *.map`), tags `repo/<name>`, records `git:<url>#<path>`, executes nothing, and is safe to re-run. It is never an
agent tool. Needs `git` (Docker build arg `WITH_GIT`). Clone timeout 300 s.

**REQ-ING-08** MAY `[U27][U32]` **OCR (off by default).** With `HIVE_OCR=true` images (`.png .jpg .jpeg .tif .tiff .bmp .gif .webp`) and PDFs without a text layer are read with
Tesseract (`tesseract stdin stdout -l <languages>`) and Poppler (`pdftoppm -r 200 -png -singlefile` per page). Startup fails if either tool is missing. Settings:
`HIVE_OCR_LANGUAGES` (default `eng`, codes joined by `+`, validated), `HIVE_OCR_MAX_PAGES` (default 50). A PDF page with fewer than 30 characters of text is a scan;
mixed documents work; if more than the page limit (and at least half the pages) need OCR the file is refused with a message to split it; a document with a few extra blank
pages is left alone; without OCR an image or scan is refused with a message that says how to enable it. Results carry tag `format/image-ocr` or `format/pdf-ocr`.
Each OCR call is a separate process as the container's unprivileged user with a 2 GiB address-space limit, 60 s per page, 300 s per document; nothing from the file reaches a
command line (files by stdin or a private temp directory); it runs in a worker thread.
*Accept:* with fake tool binaries the limits and the refusal messages are exercised; in the OCR image `tesseract --list-langs` includes `eng`.

**REQ-ING-09** MUST `[D]` **Size guards.** Tool uploads above 10 MiB, files above 50 MiB, documents above 200 parts and invalid base64 are refused with clear messages
that point to `hive ingest` where relevant.

**REQ-ING-10** SHOULD `[U22]` **CLI contexts.** CLI ingest acts as the named `--user` in `--org` with `--role` (default `member`); it does not need a token. It is the
bulk path that bypasses the assistant-facing endpoint.

**REQ-ING-11** MUST `[D]` **Linking to entities and sources** see REQ-ENT-06 and REQ-KNO-11.

**REQ-ING-15** SHOULD `[D]` **Resumable background folder ingestion (resolves OQ-15).** `hive ingest PATH --org O --user U [--project P] [--tag T] --background`
takes exactly one folder, requires that it exists now and is a directory, resolves it, refuses it if `HIVE_INGEST_ROOT` is set and the folder is not below it, queues a
durable job of kind `ingest_folder` (payload `{run_id, step}`, dedupe key `bulk:<run>:<step>`) and prints the run id and that the path must exist in the server's
container. Not an MCP tool. Each step walks up to 25 files after the saved cursor in a fixed order (depth first, names sorted; hidden entries and symlinks skipped, files
opened without following links), ingests them through the normal ingest service (REQ-ING-04 idempotency), writes the cumulative progress `{state, cursor, done, failed, skipped, bytes,
created, errors[<=20]}` under a key only one worker can create (`put_if_absent`), and queues the next step. A failing file is counted and noted; the run continues.
Plain-text, markdown and code files over 50 MiB (up to 2 GiB) are stored in 4 MiB pieces `name.partNNNN.ext`; every other format over 50 MiB is skipped with a reason.
`hive ingest-status [ID] [--org O]` shows runs and counts; `hive ingest-cancel ID` stops a run after the current file.
*Accept:* a folder is fully ingested over several steps with exact counts and cursor; stopping the runner after two steps and continuing with a new service instance ends with
the same items and counts; a second run over the same folder reports `created = 0`; a failing file is counted and listed while the others are stored; cancel yields state
`cancelled` with what was stored kept; two workers handling the same step, and three racing runners, count every file once and queue one next step; a crash between saving
a step and queueing the next is repaired when the job is retried; symlinks to files or folders outside the root are never read; a missing folder, a file, or a folder outside
`HIVE_INGEST_ROOT` is refused at once; an over-limit PDF is skipped with its reason while an over-limit text file is stored in pieces without losing a line.

**REQ-ING-12** MUST `[U34][D] **Hidden characters are removed.** Unicode tag characters (U+E0000-E007F), bidi overrides and isolates (U+202A-202E, U+2060-206F),
zero-width and joiner characters (U+200B-200F, U+FEFF), soft hyphen (U+00AD), U+034F, U+061C and U+180E are removed from titles and bodies on every write and counted as
`invisible-characters`.
*Accept:* a note containing a tag-character-encoded instruction is stored without those characters.

**REQ-ING-13** MUST `[U2][D]` **Secrets are masked before storage.** Best-effort redaction replaces matches with `[REDACTED:<rule>]` and reports the kind and
count (never the value): `aws-access-key`, `aws-secret`, `github-token`, `slack-token`, `google-api-key`, `llm-api-key` (`sk-...`), `jwt`, `bearer-token`,
`azure-account-key`, `url-password`, `credential-assignment` (api key / secret / token / passwd / password / pwd assignments, including `DB_PASSWORD=` style names),
and `private-key` (PEM blocks). PEM masking is a single linear pass (a lazy regex took seconds on a hostile body) and a block whose end is missing is masked to the end
of the text; redaction is idempotent (already-masked text is unchanged). The rule set is in Annex F.1. Redaction is documented as best effort, not a guarantee.
*Accept:* masking a 190 KB hostile body finishes in well under a second; masking twice equals masking once.

**REQ-ING-14** MUST `[U34][D]` **Prompt-injection heuristics flag, never block.** Text matching heuristics (override-instructions, role markers, fake system turn,
exfiltration request, markdown-image exfiltration, conceal-from-user; Annex F.2; matched on the NFKC-folded text) adds the tag `flag/injection` and is reported in the write
result as `injection:<heuristic>`. The tag follows the content: editing the text clean removes it, editing it back restores it. Approvers see the tag on items they are
asked to promote. Matches are flags, not rejections (heuristics can be wrong and a hard block would be easy to probe). Every regex is linear-safe (a quadratic pattern on
newlines was found by fuzzing and fixed) and adversarial inputs for each check have a time-limited regression test. The server cannot neutralise injection; it labels
external results, keeps provenance on every item and tells agents to treat retrieved text as data.

### 3.10 Curation (CUR)

**REQ-CUR-01** MUST `[U25]` **Bulk retag.** `kb_retag(ids, add_tags, remove_tags)` applies the same tag change to 1-100 distinct ids; each item reports `changed`,
`unchanged` or `failed` with a reason; one failure never blocks the others; naming no tags or the same tag in both lists is refused; items the caller cannot edit are reported,
not changed.

**REQ-CUR-02** MUST `[U25]` **Rename a tag.** `kb_rename_tag(old, new, dry_run=true)` renames a tag on every item the caller can edit (active, draft, superseded and archived;
at most 1,000); dry run by default; items that cannot be edited are reported.

**REQ-CUR-03** MUST `[U25]` **Similar items.** `kb_similar(id, limit 1-50 (default 10), min_score?)` returns items closest in meaning (embedding of the first chunk) with score,
`likely_duplicate` (score at least 0.9, or identical content hash), origin and external flag; archived items are skipped; it includes other groups' items marked external; an
item narrower than the embedding policy has no meaning-based neighbours.

**REQ-CUR-04** MUST `[U25][D]` **Merge.** `kb_merge(ids (2-20), title, body, kind?, tags?, retire)` creates one consolidated item written by the caller, linked `supersedes` to each
source; tags default to the union of the sources' tags; `retire` is `superseded` (default), `archived` or `keep`; needs edit rights on every source (nothing is stored if any
is refused); placement follows REQ-ACC-17; retrying an identical merge returns the same item (idempotency key from ids, title, body, kind and tags, *not* revisions).
Per-source failures are reported in `skipped`.
*Accept:* the same merge twice creates one item; sources become `superseded`.

**REQ-CUR-05** SHOULD `[U34]` **Duplicate sweep.** `kb_duplicates(after, scan 1-200 (default 50), threshold 0.5-1.0 (default 0.9))` sweeps the caller's editable active items
a page at a time (cursor continues), asks the vector index for each item's 6 closest neighbours at or above the threshold, joins mutually close items into groups (union-find)
and returns groups with ids, best score and `exact` (identical content). It changes nothing; merging stays a deliberate step.

**REQ-CUR-06** SHOULD `[U34]` **Stale-note review.** A note is stale when neither an edit nor a confirmation is newer than the window (`days` 7-3650, default 180); session and
compaction items are exempt. `kb_review_stale(days, limit 1-100 (default 30), after, mine_only=true)` lists stale active notes (own notes, or with `mine_only=false` anything the
caller can edit; bounded to 2,000 scanned). `kb_confirm(id)` vouches that a note is still true: only for active items the caller can edit; recorded in the KV store (`stale:confirmed`)
and the audit trail (`item.confirm`) without creating a revision. With `HIVE_STALE_REVIEW_DAYS=N` the weekly digest email gets a "Still true?" section with the reader's own
stale notes (at most 10).

**REQ-CUR-07** SHOULD `[U36]` **Conflict detection.** `kb_conflicts(id, limit 1-20 (default 5))` finds active, primary-tier notes with similarity at least 0.7 to the given note that may
contradict it: heuristics (different sets of numbers; one text containing negation words `not never no longer cannot can't don't doesn't isn't aren't won't must not without` and the
other not) and, when a model is configured, the model's verdict (`{"contradict": bool, "why": ...}`). A model "no" removes the candidate; with no model and no heuristic reason the
candidate is dropped; model-confirmed candidates sort first. The heuristic only reorders/annotates and never blocks. The agent then records the result with `kb_link relation=contradicts`
or fixes/retires one note.

**REQ-CUR-08** SHOULD `[U27]` **Digest.** `kb_digest(days 1-90 (default 7), limit 1-100 (default 30), include_external=false, project?, team?)` lists items created in the window in places the
caller can read, grouped by origin, with author, kind, tags and id, the busiest 8 tags, and (for approvers) the number of pending promotion requests; the caller's own items and
session/compaction logs are left out; `include_external` adds org-visible knowledge of other groups labelled `EXTERNAL`. It is built with the caller's own read filter. At most 2,000
items are examined (the text says when that limit was hit). `hive digest --user U [--days N] [--send]` prints a person's digest or sends this week's emails.

**REQ-CUR-09** SHOULD `[U32]` **Changed by others.** Items created before the window and edited by someone else during it are listed separately with the editor (the person who
produced the current version, found from history); the caller's own edits are not echoed; `kb_list sort=updated` browses the same way.

**REQ-CUR-10** SHOULD `[U34]` **Project and team digests.** With `project` (or `team`, not both) the digest is that group's own: everything new or changed in that scope by anyone, including the
caller's notes, with the same visibility rules; the caller must be a member (otherwise refused). Also available as resources (REQ-SESS-17).

**REQ-CUR-11** SHOULD `[U27][D]` **Weekly digest email.** With SMTP configured and `HIVE_DIGEST_WEEKDAY` (0=Monday..6) set, every enabled directory user receives their own digest by email
once per ISO week at or after `HIVE_DIGEST_HOUR_UTC` (default 8): titles, origins and ids, never item text, only items they may read; people with nothing new (and no approvals) get nothing.
A claim per person and ISO week in the KV store (`digest:sent`, 14-day TTL) gives one email per person per week across replicas and restarts; a failed send releases its claim and is retried at the next
check (every 10 minutes). The digest is not posted to a chat webhook (a shared channel would show titles of things only some members may read).

### 3.11 Retention, purge and erasure (RET)

**REQ-RET-01** MUST `[U25]` **Archive hides; purge removes.** `kb_update status=archived` hides an item from search and lists and keeps everything; `kb_purge(id, confirm)` removes
permanently: the item row, tags, idempotency records, search entries, vectors, links, promotion requests, history, and the stored log file when no other item references it. The caller must
repeat the id as `confirm`. Only the item's author (who can still write it) or an admin may purge; others see not-found unless they can read it, then a permission message. The tool is marked
destructive and idempotent.
*Accept:* after purge every one of those artefacts is gone; `confirm` mismatch is refused.

**REQ-RET-02** MUST `[U25][D]` **Crash-safe purge.** A cleanup job `purge_cleanup` (delay 60 s, dedupe key `purge:<id>`) is queued first; the item row is deleted first and the leftovers right
after; if the process dies in between, the job finishes the cleanup (and does nothing if the item still exists). Every purge is audited (`item.purge`) with ids and kind/scope, never content.

**REQ-RET-03** MUST `[U25]` **Forget a person.** `hive forget --org O --user U [--include-shared] [--erase-audit] --yes` (without `--yes` a dry run) purges a person's private items always, and
their shared items only with `--include-shared` (otherwise listed and left); an admin-only service function; audited as `user.forget`.

**REQ-RET-04** MUST `[U36]` **Full erasure.** With `--erase-audit`, the person's id is replaced by one random alias `erased-<12 hex>` (including the scope owner of old private revisions and the author and replaced-by fields of every revision) (stored nowhere; a hash would be guessable from a list of emails)
everywhere it stays: audit log (as actor and as target), authors of shared items left in place, authors and editors of earlier versions, promotion requesters and deciders, and link authors; their
idempotency keys are deleted. What may still mention them is documented (free-text audit details, a note's own text, expiring KV records, backups until expiry). The operator also removes the
account (`hive user remove`, `hive token revoke`).
*Accept:* after erasure no table contains the original id in the listed columns.

**REQ-RET-05** MUST `[U25][U36]` **Retention rules.** `HIVE_RETENTION_RULES` is a JSON list of rules `{kind?, scope?, days, action?}` with `scope` in `private|project|team|org`, `days` 1-36500 and `action`
`purge` (default) or `archive`. A rule must name a kind or a scope; unknown keys and bad values stop the server at startup. Items matching any rule and not changed for that many days (age from the
**last change**; an archive counts as a change, so an item is archived once) are purged or archived (archive uses a system status change recorded as a revision whose editor is `system`, re-indexes
the item and writes `item.archive` with actor `system:retention`). A sweep every 10 minutes handles at most 500 items in total. `hive retention` previews, `hive retention --yes` applies now. `HIVE_RETENTION_SESSION_DAYS=N`
purges session and compaction reports older than N days (0 = keep, default). Backups keep purged data until they expire (documented).

**REQ-RET-06** MUST `[D]` **Operator purge.** `hive purge ITEM_ID --org O` permanently deletes one item as an operator override.

### 3.12 Connectors (CONN)

**REQ-CONN-01** SHOULD `[U34]` **Scheduled imports in-process.** Connectors keep a project's knowledge equal to an outside source: new documents are imported, changed ones replace
their old version, vanished ones are retired (archived, never deleted). They run inside the server process (no worker, cron or queue service): a loop checks every 60 s which connectors are due;
with several replicas a claim in the KV store (`connector:run`, key `<name>:<slot>`) makes exactly one run per interval slot, and a restart inside a slot does not repeat it.

**REQ-CONN-02** SHOULD `[U34][D]` **Configuration.** `HIVE_CONNECTORS` is a JSON list; common fields `name` (`^[a-z0-9][a-z0-9_-]{0,39}$`, unique), `type`, `org`, `owner` (the identity that authors
imported items; need not be a person), `project` (items are written at project level; widening goes through promotion), `tags`, `every_minutes` (default 360, minimum 5). Unknown options, missing
required ones and non-whole-number intervals stop the server at startup. **Secrets are never in the setting**: a key file path or the *name* of an environment variable (`token_env`, `secret_env`).
Types and options: Annex I.1.

**REQ-CONN-03** SHOULD `[U34][U36]` **Types.** `urls` (fetched with the URL safety rules), `github` (REST API; markdown/text/html/pdf/Office files, no vendored or minified files, 1 MB each, 2,000 files; a file is
re-downloaded only when its blob sha changes; token for private repos; GitHub Enterprise `api_base`), `drive` (Google service account with domain-wide delegation, read-only Drive scope, as a named user;
sub-folders to depth 5, 2,000 files, 20 MiB; Docs and Slides exported as text, Sheets as CSV, PDF/Office/text downloaded; change marker checksum or modified time), `confluence` (pages of a space as HTML; marker =
page version), `jira` (issues matching JQL with status, labels, description and up to 50 comments, one note each; marker = `updated`), `notion` (every page shared with the integration or one database; blocks flattened
3 levels deep; marker = `last_edited_time`), `sharepoint` (library or OneDrive through Microsoft Graph with an Entra app; recursive to depth 5, 2,000 files; marker = eTag).

**REQ-CONN-04** MUST `[U34][D]` **Safety rules.** Nothing is retired on a bad run: if the listing fails or any document failed to download, no document is retired; if more than half of the documents (and at
least 5) vanish at once nothing is retired (`hive connectors run --force` overrides). Changed documents are ingested again and the previous parts archived (still in history); a document that is current again
is re-activated. Imported text goes through the normal sanitiser; items carry `source=connector:<name>:<key>` and tag `connector/<name>`. A people-edited imported item is not protected from the next sync (edit
the source). Document identity uses a 32-hex SHA-256 prefix of the source key; unchanged documents are detected by version marker and by content fingerprint.
*Accept:* a fake source that lists nothing leaves all items active; a source with 3 of 10 documents removed archives exactly those 3.

**REQ-CONN-05** SHOULD `[U34]` **Operation.** `hive connectors list | status | run [name] [--force]`; every run writes a `connector.sync` audit event with the counts and stores its status
(time, summary, up to 20 failures) in the KV store; the admin page `/admin/connectors` shows status and can run a sync (REQ-ADM-06). With `HIVE_RUN_JOBS=false` use `hive connectors run` from a scheduled task.

### 3.13 Optional language model (LLM)

**REQ-LLM-01** MAY `[U34][U36]` **The server needs no model.** Two curation helpers can use one when configured, each with a useful fallback: `kb_merge_propose(ids 2-8)` drafts ONE merged note (nothing is saved; refused
when no model is configured) and `kb_conflicts` uses the model's verdict for candidates (heuristics only otherwise).

**REQ-LLM-02** MUST `[U3][U36][D]` **Privacy rule.** Only notes the asker can read and that are at or wider than `HIVE_LLM_MIN_SCOPE` (default `project`) are ever sent to the model; a private note in a merge request is refused
before anything leaves the server; the rule mirrors REQ-EMB-05. Prompts and answers are not stored by the server.
*Accept:* with a fake provider recording calls, a private note produces no call.

**REQ-LLM-03** MUST `[D]` **A model's answer is only a proposal or a hint.** Notes are placed between `<note>` tags with a system instruction to treat them as data; the model's merge draft passes the usual sanitiser (secrets
masked, hidden characters removed, injection flagged) and is shown, never saved; a conflict verdict only reorders and annotates candidates; the proposal's `flagged` field says when the text looks unsafe; empty proposals are
errors; a failed or garbled model call degrades to the heuristic (conflicts) or a clear error (proposals); at most 40,000 characters of notes per merge request.

**REQ-LLM-04** MAY `[U36]` **Providers.** `HIVE_LLM_PROVIDER` is `none` (default), `openai` (any OpenAI-compatible chat endpoint via `HIVE_LLM_URL`, also Azure with `HIVE_LLM_AZURE=true`) or `anthropic`
(`https://api.anthropic.com/v1/messages`, version header `2023-06-01`, optional URL override), with `HIVE_LLM_MODEL` and `HIVE_LLM_API_KEY`; timeout 60 s.

**REQ-LLM-05** SHOULD `[U38][U40]` **Measuring the helpers.** `hive eval-assist conflicts <file>` scores contradiction detection (heuristic; with a model also the model and the pipeline as shipped: precision, recall, correct calls)
and `hive eval-assist merges <file>` scores merge drafts (required facts kept). The user asked for this to be evaluated; in this environment no provider key existed, so the exact prompts were answered by a Claude model through a
subagent and scored by the same code (24 contradiction pairs: heuristic precision 0.57, recall 0.67; model 1.00/1.00; 3 merge groups: all required facts kept). These numbers check the machinery and are not accuracy figures.

### 3.14 Authentication and identity (AUTH)

**REQ-AUTH-01** MUST `[U2][U3]` **Transport and bearer auth.** The MCP endpoint is Streamable HTTP at `POST <public_url>/mcp` (JSON responses, stateless) and serves both protocol eras (2025-11-25 clients that
use sessions, and 2026-07-28 stateless clients) from the same app. Every MCP request needs `Authorization: Bearer <token>`; a missing or invalid token gets HTTP 401 with a `WWW-Authenticate` header pointing at the OAuth
protected-resource metadata. `GET /healthz` and `GET /readyz` need no credentials. Host and Origin are validated against an allow-list (the public host, loopback, `HIVE_ALLOWED_ORIGINS`) to defeat DNS rebinding.
*Accept:* an unauthenticated call returns 401; an SDK client negotiates `2025-11-25` in legacy mode and `2026-07-28` in auto mode and calls tools in both.

**REQ-AUTH-02** MUST `[U2][D]` **Static bearer tokens.** `hive token create --label L --subject S --org O [--role R] [--team T]... [--project P]... [--approves scope]... [--expires-days N] [--rate-per-min N]`
creates `hive_<token_urlsafe(32)>`, prints the plaintext once, stores only its SHA-256 and the claims in `tokens.json` (owner-only file, atomic writes). Labels are unique and are how tokens are revoked
(`hive token revoke LABEL`, effective immediately without restart); `hive token list` shows labels and claims, never secrets. Lookup compares every record in constant time per record (no early exit). Expired tokens
are refused. The file is re-read when replaced (inode, mtime and size), so a revoked token cannot stay cached.
*Accept:* a revoked token is refused on the very next request; the plaintext is not recoverable from the file.

**REQ-AUTH-03** MUST `[U2][D]` **Tokens of people stop with the person.** A token (and an admin session) whose subject is an email address belongs to a person in the user directory and works only while that person is
present and not disabled; other subjects (service accounts, pipelines) are not checked and end only by revocation or expiry.
*Accept:* `hive user remove ann@x` makes ann's static tokens fail at once.

**REQ-AUTH-04** MUST `[U3][D]` **User directory.** `users.json` lists who may sign in and what each belongs to: email (normalised: trimmed, case-folded, at most 254 characters, a single `@`), org, role (default viewer in the file,
member via the CLI), teams, projects, `disabled`, `approves` (scope keys), and the pinned provider account id `google_sub`. `hive user add EMAIL --org O [--role R] [--team T]... [--project P]... [--disabled] [--approves S]...`,
`hive user list`, `hive user remove EMAIL`. Edits apply to live tokens and sessions at once; one unreadable row is logged and skipped without locking out others (and is preserved when the file is rewritten).

**REQ-AUTH-05** MUST `[U3][U4]` **OAuth 2.1 for claude.ai, ChatGPT, Gemini and others.** When a client id and secret are configured (`HIVE_GOOGLE_CLIENT_ID/SECRET`, names historical), the server is its own authorization server
(the MCP SDK serves discovery metadata, dynamic client registration, `/authorize`, `/token` with PKCE S256) and delegates *who you are* to the identity provider. The provider layer adds persistence, single-use codes,
refresh rotation with reuse detection, token families, live directory checks, resource binding and a consent page. Static tokens keep working next to OAuth. Scope `hive` is the only scope. Tokens are bound to this server's
resource URL (RFC 8707): an authorize request with another `resource` is refused (`invalid_target`).
*Accept:* an end-to-end test against a fake IdP completes discovery, registration, authorize, login, consent, code, token and a tool call as the right person.

**REQ-AUTH-06** MUST `[U3][D]` **Login transaction.** `/oauth/start?txn=` redirects to the IdP with `state=<txn id>`, a nonce, an upstream PKCE verifier (S256), `prompt=select_account` and (Google, single allowed domain) an `hd` hint;
the browser is bound to the transaction by a signed `HttpOnly`, `SameSite=Lax` cookie `hive_oauth` (path `/oauth`, `Secure` on https, 600 s) and the callback is refused if the cookie does not match (login CSRF);
a transaction lives 600 s. `/oauth/google/callback` exchanges the code, verifies the ID token and checks identity (REQ-AUTH-07), then shows the consent page.

**REQ-AUTH-07** MUST `[U3][D]` **What a sign-in must pass.** The ID token is verified: RS256 only (the token never picks its algorithm), signature against the provider's published keys (cached; refetched at most every 60 s on an unknown
key), issuer, audience = our client id, `exp iat iss aud sub` present, 60 s leeway, and the per-login nonce. Then: verified email; the email's domain in `HIVE_GOOGLE_ALLOWED_DOMAINS` (empty list = nobody may sign in); the account belongs to the
organisation (Google: the `hd` claim equals the email's domain; Entra: `tid` equals the configured tenant; generic OIDC: `email_verified` true, to be used only with an IdP that holds the organisation's own accounts); a non-disabled directory
entry; and the pinned provider subject, if any, equals this account's `sub`. A denial shows the client only `access_denied` and logs the reason. The first successful sign-in pins the account id; a later sign-in with the same email but another account
(an address deleted and reassigned) is refused; editing the user with `hive user add` (or the admin page's reset) clears the pin.

**REQ-AUTH-08** MUST `[U34]` **Other identity providers.** `HIVE_SSO_PROVIDER` is `google` (default), `entra` (needs `HIVE_ENTRA_TENANT`, a GUID; issuer `https://login.microsoftonline.com/<tenant>/v2.0`; scopes `openid email profile`;
the address is `email` or else `preferred_username`) or `oidc` (`HIVE_GOOGLE_ISSUER` is the issuer; must be https unless loopback). Discovery is validated (issuer must match; authorization, token and jwks endpoints present).
Multi-tenant `common`/`organizations` issuers and non-RS256 providers are not supported. Callback URLs `<public>/oauth/google/callback` and `<public>/admin/google/callback` are registered at the provider (path names are historical).

**REQ-AUTH-09** MUST `[U3][D]` **Consent.** Every login that passes shows a consent page with the application name, the host the user will be sent to, the email and scopes; the form carries its own CSRF token; the page cannot be framed and
sends no scripts. Approval is remembered for the same user + client + redirect URI + scopes for `HIVE_CONSENT_REMEMBER_DAYS` (default 30; 0 = ask every time); denials are never remembered; the IdP login and the directory check still
happen every time.

**REQ-AUTH-10** MUST `[U3][D]` **Codes, tokens and families.** Authorization codes are single use and live 300 s; replaying one revokes every token it produced. Access tokens (`hat_` prefix, 1 h, `HIVE_ACCESS_TOKEN_TTL` min 60) and refresh tokens (`hrt_` prefix,
30 days from login, not extended by use, `HIVE_REFRESH_TOKEN_TTL` min 3600) are opaque and stored only as SHA-256 hashes. Refresh tokens rotate on every use; presenting an already-rotated one revokes the whole family; a refresh cannot widen scope; a client can
revoke only its own tokens. All tokens descend from a *family*; revoking a family kills them all. Registered clients that never complete a login expire after 30 days.
*Accept:* replayed code and replayed refresh token each revoke the family (tested).

**REQ-AUTH-11** MUST `[U3][D]` **Live authorisation.** Permissions are read from the directory on **every request** (an OAuth access token carries only the person's email; role, teams, projects and approver scopes come from the directory at
that moment), so removing, disabling or demoting a user takes effect immediately; refresh and code exchange also re-check the user.

**REQ-AUTH-12** MUST `[D]` **Revocation endpoint.** `POST /revoke` implements RFC 7009 and replaces the SDK route (which demands a client secret even from public clients): it authenticates the client, refuses bodies over 64 KiB (by declared length) with
413, requires `token`, revokes only tokens belonging to the authenticated client, and answers 200 for unknown tokens. Routes of ours are placed before the SDK's so this one shadows it.

**REQ-AUTH-13** SHOULD `[U3][D]` **Registration hygiene.** Registration refuses script-like schemes (`javascript data file vbscript blob about ftp`), fragments, non-loopback `http`, hosts missing for http(s) and, when `HIVE_OAUTH_REDIRECT_HOSTS` is set,
any redirect host not on the list. Open registration is rate limited (REQ-USE-03) and operators are told to also put a rate limit in front.

**REQ-AUTH-14** SHOULD `[U29]` **Groups from Google Workspace.** With `HIVE_GROUP_MAP_FILE` (JSON: `org_id`, `auto_create`, `default_role`, `disable_missing`, `groups: {group-email: {teams?, projects?, approves?, role?}}`) the sync reads group members through the Admin Directory
API (service account with domain-wide delegation, scope `admin.directory.group.member.readonly`, impersonating `HIVE_GROUP_SYNC_ADMIN`, key `HIVE_GROUP_SYNC_KEY_FILE`) and updates `users.json`. The sync owns only the values the map mentions;
a user who leaves a group loses its teams/projects/approver scopes; the highest role among their mapped groups wins (or `default_role`); people in no mapped group keep their role; `auto_create` adds members of `HIVE_GOOGLE_ALLOWED_DOMAINS`;
`disable_missing` disables users in no mapped group (never admins; a returning user is re-enabled); nested groups count; suspended users and non-user members are ignored. **All-or-nothing:** if any group cannot be read, or returns no members
(unless `--allow-empty`), nothing changes. `hive sync-groups [--dry-run] [--allow-empty]`; optional in-server loop every `HIVE_GROUP_SYNC_MINUTES` (0 = only on command); failures logged and retried.
*Accept:* a typo in a group name changes nothing and exits non-zero.

**REQ-AUTH-15** MUST `[D]` **Secret key.** `HIVE_SECRET_KEY` (at least 32 characters) signs login and admin cookies and derives the key that seals integration secrets; if unset, a random 32-byte key is generated once into `<data_dir>/secret.key` (hex, created
owner-only with `O_EXCL`). With more than one replica a fixed key is required so all replicas verify each other's cookies.

**REQ-AUTH-16** MUST `[U22][D]` **Abuse limits on sign-in.** See REQ-USE-03.

**REQ-AUTH-17** SHOULD `[U34][D]` **Service-account tokens for Google APIs.** Google API access (Drive connector, group sync) uses a JWT-bearer grant with PyJWT (no Google SDK): RS256 assertion, scope as configured, token cached until 60 s before expiry,
failures reported as "Google refused the service account (check delegation and scope)".

### 3.15 Web admin pages (ADM)

**REQ-ADM-01** SHOULD `[U27][U34]` **Opt-in, server-rendered, no JavaScript.** `HIVE_ADMIN_UI=true` serves pages under `/admin` for people who prefer a browser. Pages use the same services and rules as the tools (no second permission path), escape every dynamic
value, send no scripts, and carry headers `Cache-Control: no-store`, `Pragma: no-cache`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin` and a CSP of
`default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`. Default is off.
*Accept:* with the setting off every `/admin` route is 404.

**REQ-ADM-02** MUST `[U27][U32][D]` **Sign-in and sessions.** `/admin/login` offers the IdP button (when configured) and a form for a pasted static token; either way the role comes from the directory or the token and is looked up again on every request.
Sessions live on the server (KV `admin:session`, keyed by a SHA-256 of a random id; the cookie `hive_admin` carries only the id plus an HMAC domain-separated from other cookies; `HttpOnly`, `SameSite=Lax` because Strict would be dropped on the IdP's
redirect back, `Secure` on https, path `/admin`); duration 8 hours; logout (`POST /admin/logout`, CSRF + Origin) destroys the session; "sign out everywhere" bumps a per-subject epoch so all earlier sessions die; a session also ends when the person is removed or
disabled or the token used is revoked or expires (token sessions are named `label#hash16`, so reusing a label does not revive an old session). Every POST needs the session's CSRF token **and** an `Origin` header equal to the public origin. Sign-ins share the
auth rate limit and are audited (`admin.login`).
*Accept:* a copied cookie stops working after logout; a POST with a wrong Origin is refused.

**REQ-ADM-03** SHOULD `[U27]` **Approvals pages.** `/admin/promotions` (approvers and admins): pending requests with the item text (preview 1,500 characters) and **Approve** / **Reject** with an optional note (`POST /admin/promotions/decide`);
`/admin/promotions/history` (approvers see their scopes, admins all): every request newest first with status, who decided, when, note; filter by status (`all pending approved rejected withdrawn stale`), 50 per page, the most recent 1,000 considered.

**REQ-ADM-04** SHOULD `[U27]` **Audit and usage pages (admins).** `/admin/audit` newest first with filters for actor, action and target, 100 per page; `/admin/usage` items and characters per person with the quota limits in force; `/admin/activity` per person calls,
writes, stored items and last seen, per-team totals and busiest tools (REQ-USE-05).

**REQ-ADM-05** SHOULD `[U32][D]` **People and tokens (admins).** `/admin/users` adds or changes a person (role, teams, projects, approver scopes), disables/enables, removes, signs out everywhere, resets the pinned account; `/admin/tokens` lists the organisation's tokens, creates one
(shown **once**, never listed again; subject, role, teams, projects, approver scopes, days 0-3650, own rate limit) and revokes. **Guards:** you cannot remove, disable or demote yourself; the last enabled admin of the org cannot be removed, disabled or demoted;
people and tokens of other orgs are invisible and untouchable; a new token belongs to the admin's org; disabling or removing ends sessions at once. Every change is audited (`admin.user.save/disable/enable/remove/signout`, `admin.token.create/revoke`); token secrets are never logged.
*Accept:* an admin cannot demote themselves; with one admin left, removing them fails.

**REQ-ADM-06** SHOULD `[U36][D]` **Operations pages (admins).** `/admin/integrations` (Slack and Teams settings: enable, workspace/tenant id, app id, signing secret, bot token, app password (write-only: shown as "stored", never displayed, sealed at rest), user map, channel-to-project
map, mentions switch; REQ-SLK-06), `/admin/connectors` (status of each connector, run now), `/admin/stale` (stale notes the admin can edit, `days` default 180, 50 shown, with a Confirm button), `/admin/duplicates` (duplicate groups from a sweep of 100 items). Mutations are POST with CSRF.

**REQ-ADM-07** SHOULD `[U43]` **Note page.** `/admin/n/<32-hex id>` shows one note read-only to **any person in the user directory** (not only admins) who may read it by the same rules as `kb_get`; someone not signed in is sent to sign in and then straight back to the note
(only paths matching `/admin/n/<32 hex>` are remembered, so the sign-in page cannot be made to redirect elsewhere; cookie `hive_admin_next`). Everything else in the admin UI keeps its own role requirement. The page exists only with the admin UI on.

**REQ-ADM-08** SHOULD `[U43]` **Legal pages.** `GET /legal/privacy` and `GET /legal/terms` (always served) describe plainly what is stored, what chat apps read, where data goes, how to ask for deletion and what use is allowed, naming the operator (`HIVE_LEGAL_ORG`, `HIVE_LEGAL_CONTACT`);
when `HIVE_PRIVACY_URL` / `HIVE_TERMS_URL` are set the pages redirect there. The text is a starting point, **not legal advice**; the operator must review it.

**REQ-ADM-09** MUST `[U27]` **Admin tools.** `kb_admin_usage` (items and characters per person, archived excluded, biggest first), `kb_admin_activity(days 1-365)`, `kb_admin_audit(actor, action, target, after_seq, before_seq, newest_first, limit 1-500)` (oldest first by default;
`before_seq` pages backwards and cannot combine with `after_seq`), `kb_admin_integration`, `kb_admin_integration_set`, `kb_admin_integration_map`. All require role admin.

### 3.16 Slack front door (SLK)

**REQ-SLK-01** SHOULD `[U34][U36]` **Ask and save from Slack.** Routes `POST /slack/commands` (`/hive <question>`, `/hive save <text>`, `/hive` or `help`), `POST /slack/interactions` (message shortcut with callback id `hive_save`, type `message_action`) and `POST /slack/events` (URL
verification challenge and `app_mention`). `/hive <question>` searches (5 results, snippets of 200 characters plus the item id); `/hive save <text>` keeps a note; the shortcut keeps a message. Replies are **ephemeral** (only the asker sees them); search runs with the asker's own access
and saved notes are private to the asker (tag `source/slack`, source `slack:<team>:<channel>:cmd` or `slack:<team>/<channel>/<ts>`, title = first line up to 80 characters, text up to 20,000 characters, idempotency by place+text+person). Saved text passes the sanitiser and
says "flagged as possibly unsafe text" when flagged. Replies escape Slack markup (`&`, `<`, `>`), so stored text cannot create `@channel`, links or formatting tricks.

**REQ-SLK-02** MUST `[D]` **Request authentication.** Every Slack request is checked **before the body is read or trusted**: HMAC-SHA256 over `v0:<timestamp>:<raw body>` with the signing secret (`v0=<hex>`); timestamps (ASCII digits) older or newer than 300 s are refused;
each signature is accepted once (KV `slack:seen`, TTL 600 s, also across replicas); bodies over 64 KiB are refused. Failures return 401. Follow-up messages go only to `https://hooks.slack.com/...` (the `response_url` Slack supplies).
*Accept:* a replayed request, a stale timestamp, a body of 70 KB and a forged signature are each refused.

**REQ-SLK-03** MUST `[D]` **Who is asking.** The Slack user's confirmed email (users.info with the bot token, scope `users:read.email`, cached 1 h, a miss 60 s; or the fixed map `HIVE_SLACK_USERS`) must belong to a person in the user directory; bots, deleted users, guests (restricted
and ultra-restricted), other workspaces and anyone not in the directory (or disabled) get a short "could not match your account" reply and nothing else. The directory check is not cached, so disabling cuts off at once.

**REQ-SLK-04** SHOULD `[U36]` **Mentions (off by default).** With `allow_mentions` enabled in the settings and the app subscribed to `app_mention`, the bot answers in a thread (via `chat.postMessage` with the bot token, links unfurling off). Because a channel is an audience, it quotes **only**
org-wide knowledge plus the notes of the channel's mapped project (and only if the asker is a member of that project); everything else is answered privately by `/hive`. Bots, edits (subtypes), other workspaces and retries of the same event (KV `slack:event`, 1 h) are ignored.

**REQ-SLK-05** SHOULD `[U36]` **Channel to project.** With `channel=project` mappings, `/hive save` and *Save to Hive* in that channel save into the project (visible to its members) instead of privately, **only for people who are members of that project**; the reply says where it went. Unmapped channels stay private.

**REQ-SLK-06** SHOULD `[U37]` **Run-time integration settings.** Slack and Teams are configured at run time, not only by environment: an admin saves settings on `/admin/integrations` or with the admin tools, and the stored settings **win** over `HIVE_SLACK_*` variables (which are only the starting point;
Teams has none). The routes are always mounted and each request consults the stored configuration, so switching on, off or to another workspace takes effect within 5 s (cache TTL) on every replica. Secrets (`signing_secret`, `bot_token`, `app_password`) are encrypted at rest with AES-256-GCM using a key derived (HKDF-SHA256, info `hive-integration-secrets`)
from the server's secret key, with the field name as associated data; they are never returned (reported only as set/not set) and **cannot be set through chat tools** (arguments appear in transcripts; the tool refuses them loudly). Undecryptable secrets (key changed) switch the integration off with a log line. Updates are audited
as `integration.update` (changed fields, never values); only admins may change settings. Enabling requires the workspace/tenant id and the secrets (Slack: signing secret and a bot token or a user map; Teams: app id and app password). The user asked for exactly this ("could that be a configurable parameter that users could manage via the admin interface or authenticated mcp chat command?"); the assistant's reading was "run-time, admin-managed settings" `[A]`.

### 3.17 Microsoft Teams front door (TMS)

**REQ-TMS-01** SHOULD `[U36][U42]` **A Teams bot in the same process.** `POST /teams/messages` receives Bot Framework activities (no extra container). It is configured at run time like Slack (REQ-SLK-06; name `teams`; the app password can only be entered on the admin
page). In a **personal chat** the person asks a question (search with their own access; 5 results, snippets of 200 characters, origin shown, referenced results included) or sends `save <text>` (a private note, tag `source/teams`, source `teams:<conversation>`, title = first line up to 80 characters,
text up to 20,000 characters). `help` or empty text returns the help text. Replies are **plain text** (`textFormat: plain`), so there is no markdown or HTML to inject into. Message text has `<at>` mention tags and other markup removed and is unescaped.

**REQ-TMS-02** SHOULD `[U36]` **Channels and group chats.** Off until an admin ticks "answer when mentioned". The audience is everyone in the conversation, so the bot quotes **only** org-wide knowledge and the notes of the project the conversation is mapped to
(`conversation-id=project`; a reply inside a channel thread `<channel>;messageid=<post>` maps through its base id); a `save` there echoes nothing it saved; saving into the mapped project works only for members of that project, otherwise the note is private to the sender.

**REQ-TMS-03** MUST `[U36][D]` **Request authentication.** Every activity must carry a Bot Framework JWT: RS256 only; signature checked against the keys published at the OpenID configuration `https://login.botframework.com/v1/.well-known/openidconfiguration` (cached 1 h; refetched at most
every 60 s on an unknown `kid`); issuer `https://api.botframework.com`; audience = the bot's application id; `exp iss aud` required; 300 s leeway; and the `serviceurl` claim must equal the activity's `serviceUrl` (that URL is where replies carrying the bot token go). Anything else gets 401.
Bodies over 256 KiB get 413; non-JSON or non-object bodies 400. Message activities are accepted once (KV `teams:activity`, TTL 1 h; redelivery is ignored). Only the configured tenant's activities (`channelData.tenant.id`) are answered. The sender (Entra object id) is mapped to an email (pinned `aad-object-id=email`
lines in the settings, else looked up through the connector's conversation member API, cached 1 h, a miss 60 s) which must be in the directory and not disabled.
*Accept:* tokens with a wrong audience, issuer, algorithm, expired, with a mismatching `serviceurl`, or signed by an unknown key are each refused (also mutation-tested).

**REQ-TMS-04** MUST `[U36][D]` **Replies go only to Bot Framework hosts.** The reply URL must be `https`, carry no credentials and the host must end with `.botframework.com` or `.botframework.us`, or be exactly `smba.trafficmanager.net` (a shared namespace such as `*.trafficmanager.net` is **not** allowed);
URL path parts are percent-escaped. The bot token is obtained with the client-credentials grant from `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token` with scope `https://api.botframework.com/.default` and cached until 60 s before expiry. Personal chat references (`serviceUrl`, conversation id) are remembered
in the KV store (`teams:ref`) the last time a person wrote to the bot, for notifications.

**REQ-TMS-05** SHOULD `[U42]` **Message extension: Search Hive.** A compose-box and search-bar command `searchHive` (type query, parameter `query`, contexts compose and commandBox): result cards (hero cards, at most 10, snippet 300 characters) to insert into a message. Because the card is shown to everyone in the
conversation, only org-wide knowledge plus the notes of the project the conversation is mapped to are offered. Card text is escaped (no links, mentions or formatting can come out of stored text: Markdown metacharacters are backslash-escaped, whitespace collapsed); each card has an **Open in Hive** button when note pages exist
(`HIVE_ADMIN_UI=true`). Queries are cleaned and cut to 200 characters; an empty or `initialRun` query asks the user to type; no results gives "Nothing shared with the whole team found for: ...".

**REQ-TMS-06** SHOULD `[U42][U43]` **Message extension: Save to Hive.** An action command `saveMessage` on any message (context message, `fetchTask`). A person who belongs to **no project** gets a one-click save of a note private to them. A person in projects gets a small Adaptive Card dialog (version 1.4, a compact choice set
"Keep it": `Private to me` (default) or `Project <p>` for each of their projects, and a **Save** submit). The message is held for ten minutes (KV `teams:save`, token `token_urlsafe(16)`) between opening the dialog and pressing Save, only for that person (a different person or an expired token gets "This dialog expired. Open Save to Hive on the message
again."); a person cannot save into a project they are not in. The note carries the sender's name ("(from <name> in Teams)") and the message link as its source (title = subject or first line, at most 80 characters); the idempotency key covers link-or-text and the place, so saving twice does nothing twice while the same message may be kept
in another place as another note. The dialog's `card` field uses the wrapped form `{contentType, content}` (matches Microsoft's TypeScript sample; flagged as the first thing to check against a real tenant).

**REQ-TMS-07** SHOULD `[U43]` **Link unfurling.** A pasted link to a note (`<public>/admin/n/<32 hex>`) unfurls into a card (command `composeExtension/queryLink`). The note's text is quoted only if everyone in the conversation may see it (org-wide, or the conversation's mapped project, and `active`); for any other note, a missing one,
or one the person cannot read, the card is the same bare "Hive note ... Open it in Hive to read it (sign-in required)" card, so it reveals nothing. The result carries `setCachePolicy no-cache` (otherwise Teams would show it to others for 30 minutes). Author and project names are not revealed. Unfurling needs the app installed (zero-install unfurling is not built).
*Accept:* a private note and a nonexistent id produce byte-identical cards.

**REQ-TMS-08** MUST `[U42][D]` **Invoke handling.** `invoke` activities (`composeExtension/query`, `fetchTask`, `submitAction`, `queryLink`) are verified like any activity, answered in the HTTP response (Teams allows five seconds), and not de-duplicated (a retry is simply answered again); other invoke names return `{}`;
a foreign tenant gets "This app is not available to your organisation."; an unmatched person gets the "could not match your Teams account" text; errors become "Sorry: <message>".

**REQ-TMS-09** SHOULD `[U42][U43]` **App package.** `hive teams-package --app-id <id> [--out hive-teams.zip] [--name Hive] [--version 1.0.0] [--link-unfurling]` (with `HIVE_PUBLIC_URL`, which must be https) writes a zip with `manifest.json` (schema v1.17; bot `botId=<app id>` for scopes personal, team, groupChat, not
notification-only, no files; `composeExtensions` with the two commands above; permissions `identity` and `messageTeamMembers`; `validDomains=[host]`; developer name and website; privacy and terms URLs `HIVE_PRIVACY_URL`/`HIVE_TERMS_URL` or `<site>/legal/privacy` and `<site>/legal/terms`; accent colour `#F5A524`) and two generated icons
(`color.png` 192x192 white hexagon ring on amber, `outline.png` 32x32 white ring on transparent). Link unfurling (`messageHandlers` of type `link` for the server's host) is added only with `--link-unfurling` or when the admin UI is on. An empty app id or a non-https public URL is refused.

**REQ-TMS-10** SHOULD `[U42]` **Honest limits.** Not built: multi-tenant bots (Microsoft no longer allows creating them), Bot Framework Emulator tokens, zero-install unfurling (`supportsAnonymizedPayloads`), Outlook support. Verified only against locally signed tokens, mocked HTTP and Microsoft's documented shapes (manifest 1.17,
`composeExtension/query`, `fetchTask`, `submitAction`, `queryLink`), not a real tenant.

### 3.18 Notifications (NOT)

**REQ-NOT-01** SHOULD `[U25]` **Promotion notifications, optional.** Promotion requests and decisions can be announced by: a chat webhook (`HIVE_NOTIFY_WEBHOOK_URL`, Slack-style: JSON `{text, event, subject, message}`; the URL is a secret and never put in error messages), email (`HIVE_SMTP_HOST`,
`HIVE_SMTP_PORT` default 587, `HIVE_SMTP_FROM` required with a host, optional `HIVE_SMTP_USER`/`HIVE_SMTP_PASSWORD`, `HIVE_SMTP_STARTTLS` default true; subject folded to one line of at most 200 characters), and (REQ-NOT-03) direct messages in Slack and Teams. All are off by default.
Messages contain the item title, who asked, where to, the reason and the outcome, **never the item body**. Requests go to the approvers of the target scope found in the directory (admins and named approvers, not the requester, not disabled users); decisions (rejected, stale, approved) go to the requester when their id is an email address.

**REQ-NOT-02** MUST `[U25][D]` **Durable, per-channel, at-least-once.** Each event and channel is a durable job `notify` (dedupe key `notify:<event>:<promotion>:<channel>:<status>`), retried with backoff by the job runner; a broken channel never blocks the others or the workflow; a notification problem never fails the request or the decision.
*Accept:* with a failing webhook the promotion succeeds and the webhook job is retried; email still goes out.

**REQ-NOT-03** SHOULD `[U36]` **Chat DMs.** Slack DMs (users.lookupByEmail then `chat.postMessage` with the bot token, scopes `users:read.email`, `chat:write`; lookups cached 24 h) and Teams proactive messages (to the personal chat remembered in `teams:ref`; people who never wrote to the bot are skipped) are always present as channels and consult the stored
integration settings when they send, so switching them on or off on `/admin/integrations` changes delivery within seconds. A chat service that is down raises an error so the job is retried; an integration that is off, or a person who cannot be found, is skipped quietly.

### 3.19 Usage, quotas and rate limits (USE)

**REQ-USE-01** SHOULD `[U27]` **Quotas.** `HIVE_QUOTA_ITEMS_PER_USER` and `HIVE_QUOTA_CHARS_PER_USER` (0 = unlimited, default) limit new items per author (characters of title + body, archived excluded). Admins are exempt. Quotas apply to new items (including reports, ingested parts, merges and chat saves) and not to edits.
A refused write says what to do ("archive or purge items, or ask an admin"). The usage query is cached 30 s per person per process (fresh on every write with several replicas); the cache is bumped after each accepted write. Archiving frees quota.
*Accept:* a member at the item limit cannot create; archiving one item allows one more.

**REQ-USE-02** SHOULD `[U27]` **Usage per person.** `kb_admin_usage` and `/admin/usage` list items and characters per author (archived excluded), biggest first, with last activity.

**REQ-USE-03** MUST `[U22][D]` **Rate limits (token buckets).** In-process buckets, shared through Postgres when `HIVE_REPLICAS > 1` (a database outage lets requests through rather than blocking); 0 disables a limit: `POST /register` 30 per hour per client address (`HIVE_RATE_LIMIT_REGISTER`);
`/authorize /token /revoke /oauth* /admin/login /admin/google /slack /teams` 60 per minute per client address (`HIVE_RATE_LIMIT_AUTH`, burst `max(per_min/2, 5)`); `/mcp` 1,200 per minute per bearer token (hash prefix `tok:` + 32 hex digits of SHA-256), anonymous calls per address, burst = 10 seconds' worth with a minimum of 10
(`HIVE_RATE_LIMIT_MCP`). Over the limit: HTTP 429, JSON `{"error":"rate_limited","error_description":"slow down and retry"}` and `Retry-After`. Health and discovery endpoints are never limited. The client address comes from `X-Forwarded-For` only for proxies listed in `HIVE_FORWARDED_ALLOW_IPS` (default `127.0.0.1`).
At most 50,000 bucket keys are kept (fully refilled ones are dropped first). The MCP rule also has a **client-address backstop** 20 times the per-token limit (rate and burst), so minting many tokens from one address cannot multiply the allowance.
Responses of limited routes carry `X-RateLimit-Limit` and `X-RateLimit-Remaining`; a 429 adds `Retry-After` and `X-RateLimit-Reset` (the shared Postgres limiter, used with several replicas, sends only `Retry-After`).
*Accept:* the 31st registration within an hour from one address is 429 with `Retry-After`.

**REQ-USE-04** SHOULD `[U34]` **Per-token limit.** A token may carry its own limit (`--rate-per-min`, 1-100,000) replacing the default for that token only; `hive token list` shows it; it is enforced per token on `/mcp`.

**REQ-USE-05** SHOULD `[U34]` **Activity meter.** Tool calls are counted in memory per (org, person, tool) and saved every 5 minutes and at shutdown as one `usage.tools` audit event per person (details: `calls`, and the top 12 `tool=count` pairs), counts only. `kb_admin_activity(days)` and `/admin/activity` add the
events up (newest first, at most 50,000 events read; the oldest are left out and `truncated` says so), plus write events (`item.create/update/relocate/purge`), stored items and characters, per-team totals (teams from the directory) and the 10 busiest tools. Prometheus metrics stay per tool, never per person.

### 3.20 Observability (OBS)

**REQ-OBS-01** SHOULD `[U29]` **Structured logs.** `HIVE_LOG_FORMAT=json` writes one JSON object per line on stderr (`ts` in ISO UTC with milliseconds, `level`, `logger`, `msg`, extra fields, `exc`); `text` (default) is human readable; `HIVE_LOG_LEVEL` sets the level; uvicorn's own lines use the same format.

**REQ-OBS-02** SHOULD `[U29]` **Request id and access log.** Every response carries `X-Request-ID`; a well-formed incoming id (8-64 characters of `A-Za-z0-9._-`) is kept, anything else replaced by a random id; the id is on every log line written while handling the request. The access log (`hive.access`, one INFO line per request) holds method, path
**without the query string**, status, duration in ms, route group and client address, and never tokens, query strings or bodies; health and metrics scrapes log at DEBUG only. Security events use logger `hive.security`.

**REQ-OBS-03** SHOULD `[U29]` **Prometheus metrics.** With `HIVE_METRICS_TOKEN` set, `/metrics` serves Prometheus text format to `Authorization: Bearer <token>` (compared in constant time; 401 with `WWW-Authenticate` otherwise); without the token the endpoint does not exist. Metrics: Annex G.3. Labels are bounded sets only (route group, method (standard
ones, else `OTHER`), tool name, job kind/state, outcome), never ids, paths or users. Job and vector gauges are computed at scrape time. Tool outcome is `ok`, `error` (expected) or `internal_error` (a bug; the log line has the traceback).

**REQ-OBS-04** SHOULD `[U29]` **Alert rules.** `deploy/prometheus-alerts.yml` ships example rules: dead jobs, job backlog older than 600 s, vector coverage below 0.95 for 30 min, 5xx rate above 2%, any tool internal error, steady 429s, many failed sign-ins, `kb_consult` p95 above 2 s.

**REQ-OBS-05** SHOULD `[U27]` **Health.** `GET /healthz` returns `{"status":"ok","version":...}`; `GET /readyz` returns `{"status":"ready"}`, 503 `{"status":"starting"}` before startup finished and 503 `{"status":"draining"}` once shutdown began.

### 3.21 Operations (OPS)

**REQ-OPS-01** MUST `[U2][U3][D]` **Soft upgrades and graceful shutdown.** On the first SIGTERM/SIGINT the server flips `/readyz` to 503 (draining) but keeps serving for a grace window (`HIVE_DRAIN_SECONDS`, default 5 s, so a load balancer can stop routing), then lets in-flight requests finish for up to
`HIVE_SHUTDOWN_TIMEOUT` (default 30 s); a second signal exits immediately. uvicorn re-raises the signal after finishing, so every piece of shutdown work (job runner and loops cancelled, final backup, storage close, embedded Postgres stop, lease release) runs inside the application's own shutdown hook, not after `run()`.
A restart of a few seconds is acceptable provided no client must be restarted: 2026-era clients are stateless, 2025-era clients are served in stateless mode, every write tool is idempotent (idempotency keys), and long work is a durable job.
*Accept:* a real-process test sends SIGTERM, observes `/readyz` 503 then exit; the exit code is -15/143 and is accepted as normal.

**REQ-OPS-02** MUST `[U2][D]` **Durable background jobs.** Jobs (kinds `index_item`, `apply_promotion`, `notify`, `purge_cleanup`, `ingest_folder`) are durable with leases and fenced completion: a claim leases a job to one worker (default lease 120 s); an expired lease can be reclaimed; the old worker can no longer complete, fail or heartbeat it; `attempts` increments on claim;
failure requeues with exponential backoff `min(30 s x 2^(attempts-1), 3600 s)` or marks the job `dead` once `max_attempts` (default 5) is reached; a dedupe key returns the existing job while one is queued or running; claiming is "oldest runnable job first" (`FOR UPDATE SKIP LOCKED` on Postgres). The in-process runner polls every 2 s, survives `StorageBusy`
(waits and continues), re-raises cancellation (the lease expires and another worker retries), and is switched off with `HIVE_RUN_JOBS=false` on replicas that only serve requests. A crash-looping job ends `dead`, not retrying forever.
*Accept:* twelve workers claiming five jobs hand out each exactly once (all backends).

**REQ-OPS-03** MUST `[U2]` **Housekeeping loops in-process.** Every 10 minutes: expired KV records are purged, the session-log retention sweep and retention rules run; usage counters flush every 5 minutes; digest check every 10 minutes; connectors every 60 s; group sync and backup on their own intervals. Housekeeping is idempotent and may run on every replica.

**REQ-OPS-04** MUST `[U27][D]` **Backup and restore.** `hive backup DEST` (file `.tar.gz` or a directory, naming `hive-backup-YYYYmmdd-HHMMSS.tar.gz`), `hive backup-verify ARCHIVE` (checksums and database integrity, restores nothing) and `hive restore ARCHIVE [--force]`. A backup is live-safe: SQLite via its online backup API (database first, then blobs, which are only ever added),
Postgres via `pg_dump --format=custom` with the password passed through the environment (never on a command line). The archive holds `db/hive.db` or `db/hive.pgdump`, `blobs/...`, `state/tokens`, `state/users`, `state/secret`, and `manifest.json` (format 1, creation time, software version, profile and the SHA-256 and size of every member); it is written to a `.part` file and renamed.
Verification refuses archives that are not readable, contain non-regular or unsafe entries (absolute names, `..`), do not match the manifest exactly, or fail a checksum, and runs `PRAGMA integrity_check` on SQLite. Restore refuses archives from the other profile, never overwrites an existing database without `--force`, always requires `--force` for Postgres (the dump replaces the tables),
restores state files with mode 0600, and must be run with the server stopped. Embedding model files are not part of a backup (`hive models fetch`); search indexes live in the database. **The archive contains secrets (token hashes, the cookie key, all knowledge in clear text): store it like the database.**
*Accept:* backup, verify and restore round-trip on SQLite and Postgres; a tampered member is detected.

**REQ-OPS-05** MUST `[U34][D]` **Self-backup for one container with a disposable disk.** With `HIVE_BACKUP_DIR` set (a durable mount): every `HIVE_BACKUP_EVERY_MINUTES` (default 15) a verified archive `hive-auto-<UTC time>.tar.gz` is written there, only if anything changed (files touched since the last backup began; for Postgres, the database's insert/update/delete
counters changed) and the newest `HIVE_BACKUP_KEEP` (default 8) are kept; a **final backup is taken when the server stops**; on start with an empty disk (no `hive.db`, or a fresh embedded Postgres cluster) and archives present, the newest archive that verifies is restored before serving (a damaged newest one is skipped for the one before). The data stays on fast local disk (SQLite must not live on a
network share); the share holds only archives. The price: a hard crash loses the changes since the last archive (default at most 15 minutes), which the operator accepted/was told (open question OQ-01).
*Accept:* a replacement container started on an empty disk comes back with its data; a corrupt newest archive is skipped.

**REQ-OPS-06** MUST `[U34][D]` **One writer at a time (lease).** The running instance holds a lease file `.hive-lease` in the backup directory (JSON `{owner, pid, revision}`, refreshed every 10 s, deleted on clean stop). A second instance finding a live lease waits up to `HIVE_BACKUP_LEASE_WAIT` seconds (default 90, polling every 2 s) then refuses to start with a clear message;
a lease untouched for 45 s belongs to a dead instance and is taken over; if two start at once the last writer wins and the loser sees it; a running instance that loses its lease logs an error.
*Accept:* two instances on one directory: the second refuses; after a hard kill the lease is taken over after 45 s.

**REQ-OPS-07** SHOULD `[U36][D]` **Zero-downtime hand-off.** With `HIVE_BACKUP_HANDOFF=true` (requires a revision id: `HIVE_REVISION`, or Azure's `CONTAINER_APP_REVISION`) a starting instance of a *different* revision asks the lease holder to yield by writing `.hive-handoff`; the holder (watching every second) records its revision in `.hive-retired` (entries kept 24 h), stops itself with a graceful
SIGTERM-equivalent (draining, final backup, storage close) while continuing to heartbeat, and releases the lease; the newcomer then restores that backup and serves, usually within seconds with nothing lost. A retired revision refuses to start again (so the platform restarting the old container does not steal the lease back). Without hand-off an overlapping start fails loudly and the old instance keeps serving.

**REQ-OPS-08** SHOULD `[U36]` **Embedded PostgreSQL in the same container (optional).** Image build arg `WITH_POSTGRES=true` adds PostgreSQL and pgvector (at least 0.7, `halfvec`; checked at build time). `HIVE_EMBEDDED_POSTGRES=true` selects the external profile against a database the server creates and supervises itself: cluster in `<data>/pg` (initdb `-U hive -E UTF8 --auth-local=trust --auth-host=reject`),
listening on a unix socket only (`/tmp/hive-pg`, no network port, no password), database `hive`; started before and stopped (fast, clean) after the server, so after the final backup; a stale `postmaster.pid` is removed first; start timeout 90 s; it refuses to start together with `HIVE_DATABASE_URL`, `HIVE_PROFILE=embedded` or `HIVE_REPLICAS>1`. Backups and restore work as in REQ-OPS-05 with `pg_dump`.
The user chose **no watchdog** (ledger U38): if Postgres dies while the server runs, readiness fails and the platform restarts the container.
*Accept:* CI builds the image and runs the single-container smoke test with the embedded database.

**REQ-OPS-09** SHOULD `[U42][D]` **Migration between SQLite and Postgres.** `hive migrate to-postgres (--database-url URL | --embedded) [--dry-run] [--no-reindex]` and `hive migrate to-sqlite --data-dir DIR [--dry-run] [--no-reindex]` copy a whole knowledge base in both directions through a verified process (the user chose "both directions, via a verified archive"; the implementation reads both stores through one table description, not through the backup archive format).
The source is only read (SQLite read-only, Postgres in a read-only transaction); the target must be empty and different from the source; copied tables: items (with tags), item_revisions, idempotency, audit (numbering kept), kv (expired records skipped), promotions, edges, plus stored files (each verified against its SHA-256); jobs, rate buckets and search/vector indexes are not copied.
After the copy both sides are read back and compared per table (row counts and an order-independent checksum) and stored files compared; any difference exits non-zero naming the table. Only then are search indexes rebuilt by embedding every note (unless `--no-reindex`). `to-sqlite` also copies `tokens.json`, `users.json` and `secret.key`. `--dry-run` only counts. Batch size 500.
Archives from `hive backup` cannot be restored into the other profile (use this command). The LanceDB store is not populated (run `hive reindex`).
*Accept:* SQLite to Postgres 16 and back with a varied dataset yields identical checksums and working search at both ends.

**REQ-OPS-10** MUST `[U2][U5][U34]` **Single image, three ways to run.** One Docker image runs: locally in one container (`docker run -v hive:/data -p 8080:8080`), on a VM, and as a single-container cloud app; no sidecar is required (no separate database, queue, worker, scheduler or tunnel container). Default profile `embedded` (SQLite + files under `/data`); `external` (Postgres + pgvector via
`HIVE_DATABASE_URL`) for scale and multiple replicas. `docker-compose.yml` is a convenience for local use with optional profiles (Annex K.2). The user explicitly rejected a many-container compose as the deployment model (ledger U34).
*Accept:* the image smoke tests start the server, mint a token, call tools, restart with data kept and exit 0 on SIGTERM.

**REQ-OPS-11** SHOULD `[U34]` **One-command demo.** `hive demo` creates, when the knowledge base is empty, a project `demo` in org `demo`, one admin token labelled `demo` (printed once; if the label exists it says so) and five example notes (project scope), prints the `claude mcp add --transport http hive <url> --header "Authorization: Bearer <token>"` line and the endpoint, then serves.
It is for demonstrations only (it prints a token).

**REQ-OPS-12** SHOULD `[U2][U23]` **Azure deployment.** Two Bicep templates (Annex K.3): `deploy/azure/single.bicep` (one Container App with one replica, an Azure Files share for archives, registry, logs; 60 s termination grace for the final backup; hand-off on; no database; settings `HIVE_PROFILE=embedded`, `HIVE_DATA_DIR=/data`, `HIVE_BACKUP_DIR=/backups`...; extra settings by parameter)
and `deploy/azure/main.bicep` (Container Apps + PostgreSQL Flexible Server 16 with `VECTOR` allow-listed + Azure Files + registry + Log Analytics; one replica; 30 s grace). Both deploy in two steps (infrastructure with `deployApp=false`, `az acr build`, then the app). Upgrades of the single-container variant are hand-off or stop-then-deploy (`scripts/azure_upgrade_single.sh`).
Status is stated honestly: built in CI but **never deployed to a real subscription**.

**REQ-OPS-13** SHOULD `[U3][U23]` **Private network plus tunnel.** The container binds loopback by default in compose (`HIVE_BIND=127.0.0.1`); documented ways to expose it to cloud assistants: a Cloudflare tunnel (`--profile cloudflare`, token) or Tailscale (`--profile tailscale`, `deploy/tailscale-serve.json`, Funnel only if cloud assistants must reach it) or the cloud's own ingress; behind a proxy
`HIVE_FORWARDED_ALLOW_IPS` must list the proxy. Claude.ai/ChatGPT/Gemini need a publicly reachable https URL.

**REQ-OPS-14** SHOULD `[U29]` **Multi-replica readiness (external profile).** `HIVE_REPLICAS=N>1` requires the external profile and a fixed `HIVE_SECRET_KEY`; shared through Postgres: items, indexes, OAuth state, jobs (each leased to one worker), audit, stored logs (blobs are rows in a `blobs` table), rate limits (`rate_buckets`) and quota reads (fresh on every write);
`tokens.json` and `users.json` must be on a share all replicas see; sticky sessions are not needed. Embedded SQLite and embedded Postgres refuse more than one replica.

**REQ-OPS-15** MUST `[D]` **Schema migrations are forward-only and guarded.** SQLite: `PRAGMA user_version`, each migration in its own `BEGIN IMMEDIATE` transaction; Postgres: table `schema_migrations`, all pending migrations in one transaction under advisory lock `727274`, creating the `vector` extension if the role may. Opening a database newer than the software is refused with a clear error.
Several app instances can start together (the earlier `CREATE SCHEMA` race was fixed with the advisory lock).
*Accept:* 40 concurrent starts against one Postgres produce no failure.

**REQ-OPS-16** SHOULD `[U27]` **Image properties.** Multi-stage build on `python:3.12-slim` (arg `PYTHON_IMAGE`), uv 0.13 (`ghcr.io/astral-sh/uv:0.13`), locked install, non-root user `hive` (uid 10001), `/data` volume, port 8080, `STOPSIGNAL SIGTERM`, healthcheck calling `/healthz`, models optionally baked in. Build args and their effects: Annex K.1.
*Accept:* CI builds and smoke-tests the image; Trivy reports no fixable HIGH/CRITICAL.

**REQ-OPS-17** SHOULD `[D]` **Analytics export.** `hive export --org ORG --out DIR [--format jsonl|parquet] [--include-private] [--include-archived] [--since ISO] [--pseudonymise]` writes `items`, `edges` and `manifest.json` (Annex D, D.2). Reads through the storage ports in pages (items by id with `ItemMaintenance.find_system`, links with `EdgeRepository.list_page`), so memory is bounded by the page size, not the corpus. Private notes are excluded unless `--include-private` (the command then prints a red warning); archived items unless `--include-archived`; a link is exported only if both ends are, so no id of a left-out note appears. Text is exported as stored (never unmasked). Parquet needs the optional `parquet` extra (pyarrow) and fails with a clear message otherwise; JSONL has no dependency.
*Accept:* a default export contains no private note and no link to one; `--include-private` and `--include-archived` bring them back independently; with `--since` only items changed (links created) on/after it are written; the same results on memory, SQLite and PostgreSQL (`conformance/test_export.py`); more than one page exports completely with every storage read bounded by the page size.

**REQ-OPS-18** MUST `[D]` **Export is private, verifiable and audited.** The directory (0700) and its files (0600) are written to a staging directory and renamed on success; a failure leaves nothing behind and an existing non-empty target is refused. `manifest.json` records schema version, org, creation time, flags, row counts and the SHA-256 and size of each file. `--pseudonymise` replaces author ids (and the owner of private scopes) by `p_` plus a salted hash, stable within the export; the salt is random per run and never stored. Each run appends an `export.run` audit event (actor `cli:<os user>`) with format, flags and counts, never content.
*Accept:* recomputing each file's SHA-256 equals the manifest; aliases are equal for one person within an export and differ between exports; the audit event holds no note text.

### 3.22 CI and security tooling (CI)

**REQ-CI-01** MUST `[U2]` **Lint, types, size, tests on every push.** GitHub Actions workflow `ci` (push, pull_request, manual, weekly `17 5 * * 1`): job `test` (Postgres `pgvector/pgvector:pg16` service, Tesseract and Poppler installed, a PG16 client first on PATH) runs `uv sync --frozen`, `ruff format --check .`, `ruff check .`, `mypy src clients/hooks/src`, `scripts/check_file_size.py src clients/hooks/src`
and `pytest`. Ruff selects `E F I B UP SIM ASYNC S PL RUF TCH ANN` with line length 100 and target py312; mypy is strict with the pydantic plugin; pytest runs with `asyncio_mode=auto`, a 60 s per-test timeout and coverage reporting.

**REQ-CI-02** MUST `[U2]` **File size guard.** `scripts/check_file_size.py` fails (exit 1) for any Python file above 16 KiB, warns above 12 KiB, target 8 KiB; it covers `src` and the client sources; tests, generated and benchmark files are not covered; ruff's per-file `E501` ignores exist only where agent-facing or SQL text reads better unwrapped.
*Accept:* a unit test of the guard.

**REQ-CI-03** SHOULD `[U29][U34]` **Supply-chain checks.** Job `audit` runs `pip-audit` on the locked dependencies (also weekly); job `image` builds the image (model baked in), runs both smoke scripts (`smoke_docker.sh`, `smoke_single_container.sh`), a variant with `WITH_POSTGRES=true`, a variant with the real ONNX embedder, builds `WITH_OCR=true` and starts it with `HIVE_OCR=true`, scans with Trivy `0.58.0` (fixable HIGH/CRITICAL only fail), and validates the compose file;
Dependabot proposes updates weekly for `uv`, `docker` and `github-actions` (the user asked for the existing Dependabot PRs to be handled; the assistant applied the bumps itself and closed them).

**REQ-CI-04** SHOULD `[U29]` **Client package check.** Job `client` builds `hive-hooks`, installs it alone in a clean venv, asserts `hive_mcp` is not imported and runs `hive-hooks hooks install --scope project --dry-run`.

**REQ-CI-05** SHOULD `[U36]` **Bicep compile.** Job `bicep` compiles `main.bicep`, `single.bicep` and `main.bicepparam`.

**REQ-CI-06** SHOULD `[U36][U42]` **Mutation testing.** Mutation testing (`mutmut` >= 3.8, dependency group `mutation`, config in `[tool.mutmut]`) covers security-critical modules: Slack signature checks, prompt-injection heuristics, retention rules, lease and hand-off, ranking nudge, Teams verification, connector and message extension (including cards), static tokens, OAuth token service, cookies,
revocation, connector settings and the sync loop (Annex L.2); the user chose to widen it to "OAuth and token code" and "Teams verification and connector safety rules" (ledger U42). `scripts/mutation.sh` exits non-zero if any mutant survives; CI runs the whole set weekly and on demand (job `mutation`), and on every pull request and push to main only the modules that change touched (`scripts/mutation_changed.sh`, job `mutation-changed`). Unobservable mutants carry `# pragma: no mutate` with a reason and must stay rare;
mutants in messages that tell a person what to do are killed with exact-text assertions. At the time of writing 2,114 mutants were all killed.

**REQ-CI-07** SHOULD `[U36]` **Fuzzing and adversarial tests.** `tests/fuzz` (Hypothesis plus adversarial inputs) covers the redactor, injection heuristics, tokenizer, extractors (PDF, Office, HTML), the URL checks and parsers with time limits; findings become regression tests (for example the quadratic injection pattern).

**REQ-CI-08** SHOULD `[U44]` **Stress tests.** `tests/stress` exercises concurrency and scale (access filter under load, knowledge concurrency, KV and jobs, promotion races, rate-limit stress); `HIVE_STRESS_SCALE=<n>` multiplies their size; marker `stress`.

**REQ-CI-09** SHOULD `[U2][D]` **Pre-commit.** `.pre-commit-config.yaml` runs ruff format, ruff check --fix, mypy on `src` and the file-size guard.

**REQ-CI-10** SHOULD `[U32][D]` **Release workflow.** See REQ-SESS-14.

### 3.23 The MCP interface as a whole (MCP)

**REQ-MCP-01** MUST `[U2][D]` **Tool surface.** The server exposes exactly the tools listed in Annex A.1 (50 at the time of writing) with the stated arguments, defaults, limits, read/write annotations and result shapes. Reads carry `readOnlyHint=true, openWorldHint=false`; writes carry `readOnlyHint=false, destructiveHint=false, idempotentHint=false, openWorldHint=false`;
`kb_purge` is `destructiveHint=true, idempotentHint=true`. Tool names are stable; optional fields are only ever added.
*Accept:* a contract test compares the generated tool schema (names, arguments, annotations) against the committed snapshot.

**REQ-MCP-02** MUST `[D]` **Errors an agent can act on, and nothing else.** Expected failures (`ValidationError`, `NotFound`, `PermissionDenied`, `Conflict`, `IdempotencyConflict`, `StorageBusy`, `EmbeddingUnavailable`, `QuotaExceeded`, `NotifyError`) become readable tool errors: not-found/not-visible identical text; conflicts say to re-read with `kb_get` and retry with the current revision;
`StorageBusy` says "temporarily unavailable; retry in a few seconds"; any unexpected exception is logged with traceback and reported as "internal error; the operation was not completed" without internals. Every tool call is timed and counted by outcome (`ok`, `error`, `internal_error`) and counted in the per-person meter.

**REQ-MCP-03** MUST `[D]` **Startup ordering.** The ASGI app serves health endpoints immediately; tool calls made before the service stack exists get the retryable "starting up" error; `/readyz` is 503 `starting` until the lifespan finished.

**REQ-MCP-04** MUST `[D]` **App composition.** Middleware: request id/access log/metrics outermost, then rate limiting; our routes (sign-in, admin, legal, Slack, Teams) are placed before the SDK's so ours win; the metrics route exists only with a scrape token; DNS-rebinding protection is on.

**REQ-MCP-05** MUST `[U2][D]` **Both protocol eras, stateless.** `stateless_http=True`, `json_response=True`, MCP Python SDK `mcp>=2,<3` (`MCPServer`, `streamable_http_app`).

---

## 4. Non-functional requirements

### 4.1 Security and privacy principles (NFR-SEC)

**REQ-NFR-SEC-01** MUST `[U3][D]` **Private by default; sharing is explicit.** Nothing is visible beyond its author without an explicit act (share to a project the person belongs to, or an approved promotion). Defaults of every writer (agents, hooks, Slack, Teams, ingestion, CLI) are private. (REQ-KNO-09, REQ-ACC-06/07)

**REQ-NFR-SEC-02** MUST `[U2][D]` **Identity only from the connection.** Identity, org, role and memberships are taken from verified token claims or the directory, never from tool arguments, headers set by clients, chat message text or request bodies. Chat front doors map a platform identity to a directory person through a verified channel (confirmed email) and then act with that person's rights.

**REQ-NFR-SEC-03** MUST `[D]` **Org isolation everywhere.** Every query, index, blob path, KV key that holds user data, audit event, promotion, edge and cache is scoped by org; blob paths use the hex of the org id so no path trick is possible; system (non-user) reads (`get_system`, `list_ids_system`, `find_system`, `relocate_system`, `set_status_system`, `purge_system`) are not reachable from any tool and never cross orgs.

**REQ-NFR-SEC-04** MUST `[D]` **Secrets never leak.** Static tokens and OAuth tokens are stored only as SHA-256 hashes; integration secrets are AES-256-GCM sealed; token secrets, signing secrets, bot tokens, app passwords, webhook URLs and database passwords are never logged, never returned by any tool or page (reported only as set/not set), never put in error messages, and never put in a command line (Postgres passwords go through the environment);
settings that hold secrets use `SecretStr`; the access log omits query strings; the admin log of token creation records labels, never secrets. Secrets are masked in stored text on every write (REQ-ING-13) and in recorded search queries (REQ-SRCH-14).

**REQ-NFR-SEC-05** MUST `[D]` **Output escaping.** Every dynamic value in HTML is escaped; no page runs scripts (CSP `default-src 'none'`); chat replies escape the target's markup (Slack mrkdwn escapes, Teams plain text, card text with Markdown metacharacters escaped); email subjects are folded to one line (no header injection); stored text can never create mentions, links or formatting in a reply.

**REQ-NFR-SEC-06** MUST `[D]` **SSRF defences.** Any server-side fetch of a user-influenced address (URL ingestion, connectors of type `urls`) follows REQ-ING-06; Slack follow-ups go only to `hooks.slack.com`; Teams replies only to Bot Framework hosts (REQ-TMS-04); Git ingestion is operator-only; connectors call only fixed API hosts or configured base URLs.

**REQ-NFR-SEC-07** MUST `[D]` **Resource-bomb defences.** File size caps (10 MiB tool, 50 MiB CLI, 20 MiB URL, 1 MiB git file, 5 MiB session log), page and part caps (2,000 PDF pages, 200 parts, 5,000 Office members), Office zip/XML-bomb protection (member and total size caps, DTDs refused), parsers off the event loop, OCR in rlimited subprocesses with time limits,
request body caps (revoke 64 KiB, Slack 64 KiB, Teams 256 KiB), regexes written to be linear (PEM masking is a hand-written pass; injection patterns use bounded quantifiers and `[ \t]`), bounded listings (2,000 scanned in digests/stale/entities, 1,000 renames, 100 bulk), and bounded rate-limit state (50,000 keys).

**REQ-NFR-SEC-08** MUST `[D]` **Authentication hardening.** PKCE S256 mandatory; exact redirect-URI matching; single-use codes; refresh rotation with reuse detection; constant-time comparisons for secrets, signatures and tokens (`hmac.compare_digest`); algorithm pinned to RS256 for ID tokens and Bot Framework tokens; nonce on every login; replay protection for Slack signatures and Teams activities;
signed, `HttpOnly`, `SameSite=Lax` cookies bound to the transaction; CSRF tokens and Origin checks on every admin POST; server-side revocable admin sessions; live permission checks on every request; cookie keys at least 32 characters and domain-separated per purpose.

**REQ-NFR-SEC-09** MUST `[U3][U29][D]` **Hosted-provider privacy.** Content that would leave the server for a hosted embedder or language model is restricted by scope (REQ-EMB-05, REQ-LLM-02); search queries go to a hosted embedder and that is documented; a local model keeps everything inside; prompts and answers of the language model are not stored.

**REQ-NFR-SEC-10** MUST `[D]` **Prompt-injection posture.** Stored content is untrusted data. The server never emits server-generated instructions inside data; external results are labelled and carry provenance; injection-like text is flagged (not rejected); invisible characters are stripped; instructions tell agents to treat retrieved text as data; approvers see the `flag/injection` tag.

**REQ-NFR-SEC-11** SHOULD `[U29]` **Public-endpoint hygiene.** Origin/Host validation, per-token and per-address rate limits, response size caps (snippets, truncation), audit of access-relevant actions, a recommendation to front the public endpoint with a WAF or managed ingress, optional redirect-host allow-list for OAuth clients, and no personal data in metrics.

**REQ-NFR-SEC-12** SHOULD `[U29]` **Reviews and scans.** Two adversarial review passes were done (OAuth/admin/backup/URL fetch; then chat front doors, connectors, integrations, model, leases) with the findings recorded in `docs/HARDENING.md`; dependency audit and image scanning run in CI; Dependabot is on.

### 4.2 Reliability (NFR-REL)

**REQ-NFR-REL-01** MUST `[U2]` **No data loss on planned stops.** A graceful stop finishes in-flight requests, takes the final backup (when configured), closes storage and releases the lease; pending index/notify/apply/purge work is durable and resumes after restart.

**REQ-NFR-REL-02** MUST `[U2]` **Clients survive restarts.** Restarts take seconds; stateless transport means no client needs restarting; non-in-flight requests are not lost; in-flight ones are retried safely thanks to idempotency keys and compare-and-set decisions.

**REQ-NFR-REL-03** MUST `[D]` **One writer.** SQLite and the embedded Postgres have exactly one writer instance (lease); several replicas are supported only with an external Postgres (REQ-OPS-14).

**REQ-NFR-REL-04** MUST `[D]` **Failure containment.** A failing embedder, notifier, connector, model, group sync or chat service never fails the primary operation: items are stored and indexed later; notifications retry independently; a bad connector run changes nothing; a failing LLM falls back to heuristics; a database lock is a retryable error (`StorageBusy`), never a crash;
one malformed directory row never locks out other people; undecryptable integration secrets switch only that integration off.

**REQ-NFR-REL-05** MUST `[D]` **Crash safety of multi-step operations.** Approvals (REQ-ACC-11), purges (REQ-RET-02), history capture (same transaction), migrations (transactional), backups (`.part` then rename; verify after write), connector state (KV), and index replacement (all-or-nothing per item and model) are crash-safe.

**REQ-NFR-REL-06** SHOULD `[D]` **Honest durability statement.** The embedded profile loses at most the backup interval on a hard crash; the external profile's durability is the database's (managed PITR in the Azure template: 14 days). Purged data remains in backups until they expire.

### 4.3 Performance and scale (NFR-PERF)

**REQ-NFR-PERF-01** SHOULD `[U3]` **Scale target.** Data in the range of gigabytes, "maybe 100 GB" (a mix of raw files and searchable text; size for the worst case where all text is embedded). The embedded profile is intended for the low tens of GB / up to a few hundred thousand notes; the external profile (Postgres + pgvector) is the scale path.

**REQ-NFR-PERF-02** SHOULD `[U10][D]` **Storage choice follows measurement.** The user asked to test the candidate stores separately to see which performs better (ledger U10). Results (Annex M, `docs/BENCHMARK.md`; 4 vCPU, 15 GB RAM): at 1M synthetic 384-d chunks pgvector (HNSW m=32, ef_construction=200, ef_search=200) recall 0.983 at 4.1 ms median, filtered 0.975 at 7.2 ms median (p99 408 ms);
LanceDB IVF_FLAT recall 1.00 at 24.5 ms; DuckDB exact 184 ms; sqlite-vec exact 611 ms; keyword search 0.2 ms (Postgres GIN), 0.3 ms (SQLite FTS5), 4.4 ms (LanceDB), 126 ms (DuckDB). On 220,000 real embeddings: pgvector recall 0.990 at 6.2 ms; SQLite exact 133-138 ms.
Decisions: Postgres + pgvector (`halfvec`, HNSW m=32, ef_construction=200) is the external profile's vector store; SQLite with exact scan is the embedded default (about 130-140 ms at 220k chunks); DuckDB and LanceDB are not primary stores (LanceDB is an optional vector store, REQ-EMB-11).

**REQ-NFR-PERF-03** SHOULD `[D]` **Interactive latency.** A `kb_consult` on the embedded profile at tens of thousands of chunks completes in well under a second (p95 alert threshold 2 s); startup to readiness in seconds (model loading excluded); indexing and embedding never block the event loop (worker threads).

**REQ-NFR-PERF-04** MUST `[D]` **Filtered recall.** Vector search must reach full recall for callers who may see a tiny fraction of the data (0.3% of 220,000 vectors): the access filter is applied before the nearest-neighbour choice; Postgres over-fetches chunks and deepens up to four rounds so one item with many chunks cannot crowd out others, with `hnsw.ef_search` set to `min(max(fetch,100),1000)` and `hnsw.iterative_scan = relaxed_order`.

**REQ-NFR-PERF-05** SHOULD `[D]` **Bounded work per request.** Every pattern applied to untrusted text is bounded or linear (the Teams `<at>` tag, the Slack mention, the image-exfil rule, the PEM key pass, JWT detection); a megabyte of repeated openers must not cost seconds. A request body over its limit is refused before any processing; deeply nested JSON on the Teams route is a 400, not a 500; at most 64 query terms are searched. Listings, scans and sweeps have explicit caps (REQ-NFR-SEC-07), so a very large knowledge base cannot make one request unbounded; bulk ingestion is a CLI path that bypasses the assistant-facing endpoint.

**REQ-NFR-PERF-06** SHOULD `[U44]` **Stress and soak.** Concurrency and scale behaviour is covered by the stress suite; real soak/load numbers at hundreds of thousands of notes under concurrent agents were **not** measured (OQ-07).

### 4.4 Code quality constraints (NFR-QA)

**REQ-NFR-QA-01** MUST `[U2]` **Small files.** Python files target 8 KiB, warn at 12 KiB, fail the build at 16 KiB; larger modules are split by responsibility, not arbitrarily. The user asked "8-10 kb per file as an aim; if 8 kb is too low what is justified? 15? 20?" (ledger U3); the assistant recommended 8 KiB target with a 16 KiB hard limit and 12 KiB warning; the user accepted by proceeding `[D]`.

**REQ-NFR-QA-02** MUST `[U2]` **Strict typing and lint.** Python 3.12; full type annotations; `mypy --strict` (pydantic plugin) clean for `src` and `clients/hooks/src`; ruff format and lint clean with the selected rule set; no `Any` in public signatures except for transparent wrappers with a justification.

**REQ-NFR-QA-03** MUST `[U2]` **Write it once.** Code with similar functions is shared: one visibility rule (`domain/access.py`), one tag normaliser, one tokenizer, one sanitiser, one chunker, one fusion function, one idempotency/fingerprint scheme, one write-authorisation module for all repositories, one `guarded` error mapper, one OAuth page shell and header set, one URL-safety implementation, one directory/token store; CLI commands and MCP tools call the same service methods.

**REQ-NFR-QA-04** MUST `[U2][U15]` **Ports and adapters with one conformance suite.** Services depend only on ports; backends (in-memory reference, SQLite+filesystem, Postgres+pgvector, optional LanceDB vectors) must pass the same conformance suite in `tests/conformance`; the in-memory adapter is the executable specification; Postgres tests are skipped cleanly when `HIVE_TEST_PG_DSN` is not set and run in CI.

**REQ-NFR-QA-05** MUST `[U2][D]` **Proactive bug hunting.** The user asked to "think proactively for bugs, and to correct them". Practices that must be preserved: every found bug gets a regression test that fails on the old code and passes on the fix; critical tests are validated by deliberately breaking the code (fault injection / mutation testing; REQ-CI-06); property-based tests (Hypothesis) for tags, chunking, scope predicates, cursors and sanitisers; fuzzing of untrusted-input parsers; repeated full-suite runs to expose flakiness.

**REQ-NFR-QA-06** MUST `[U44]` **Review, tests and docs on request.** The final request asked for a careful code review for bugs, regression tests for every bug found, stress tests, and documentation so that the requirements alone suffice to regenerate the code (this document and its Annex), plus README, usage instructions, use cases and an architecture document.

**REQ-NFR-QA-07** SHOULD `[D]` **Test layout.** Tests mirror areas: `tests/unit`, `conformance`, `adapters`, `services`, `security`, `integration` (real app + SDK client + fake IdP + real process + Docker-free single-container tests), `webui`, `slack`, `teams`, `connectors`, `integrations`, `ops`, `client`, `fuzz`, `hardening`, `regression`, `stress`, `embeddings`; fakes of Google, Slack, Teams, Atlassian, Notion, GitHub, Drive and Graph are used; a docs-drift test keeps documents and code in step.

**REQ-NFR-QA-08** SHOULD `[D]` **Injectable time and randomness.** Services use an injectable clock and fixed seeds in tests so time-dependent behaviour is deterministic.

### 4.5 Documentation (NFR-DOC)

**REQ-NFR-DOC-01** MUST `[U2]` **Docs ship with the features.** Every feature has a page under `docs/` (one file per feature: ADMIN, AGENTS, BACKUP, BENCHMARK, CONNECTORS, CURATION, DEPLOY, DIGEST, EMBEDDINGS, ENTITIES, EVALUATION, GRAPH, GROUPS, HARDENING, HISTORY, INGEST, LLM, MCP, MIGRATE, OAUTH, OBSERVABILITY, PLAN, PROMOTION, RETENTION, SERVICES, SINGLE-CONTAINER, SLACK, STORAGE, TEAMS) and the README indexes them; honesty rule: each page states what was verified, what was tested only against fakes and what is not built.

**REQ-NFR-DOC-02** MUST `[U44]` **Final documentation set.** README (what it is, quick start, how to use, use cases), an architecture document (`docs/ARCHITECTURE.md`), and this requirements specification with its annex (`docs/REQUIREMENTS.md`, `docs/REQUIREMENTS-ANNEX.md`).

**REQ-NFR-DOC-03** SHOULD `[U2]` **Plan and decisions.** `docs/PLAN.md` keeps the research findings, revisions and decisions (original plan; revisions 2-4 record the answers and the single-container change; its status banner says where the pages win).

**REQ-NFR-DOC-04** SHOULD `[D]` **Docs honesty on status.** The README states that the system is feature complete for a first deployment but **not verified against real Google/Entra, real assistants (claude.ai, ChatGPT, Gemini), real Slack/Teams tenants, Azure, Atlassian/Notion/SharePoint**, the Claude Code hook settings inside a real session, or at large scale (OQ-02..OQ-08).

---

## 5. Technology and packaging constraints

**REQ-TECH-01** MUST `[U2]` **Language and tooling.** Python (>= 3.12; `.python-version` 3.12), managed with **uv** (lockfile `uv.lock` committed; the user confirmed that code, dependencies and environment are managed by uv, ledger U39), `src/` layout, `pyproject.toml` only, ruff, mypy strict, pytest, pre-commit.

**REQ-TECH-02** MUST `[D]` **uv workspace with two distributions.** Root package `hive-mcp` (module `hive_mcp`, command `hive` -> `hive_mcp.cli:main`, build backend `uv_build`) and workspace member `clients/hooks` = `hive-hooks` (module `hive_hooks`, command `hive-hooks` -> `hive_hooks.cli:main`); `hive-hooks` is a dependency of `hive-mcp` (workspace source). Versions are kept equal (0.1.0 at the time of writing).

**REQ-TECH-03** MUST `[D]` **Runtime dependencies.** `hive-hooks`, `httpx>=0.27`, `mcp>=2,<3`, `numpy>=2`, `prometheus-client`, `pydantic>=2`, `pydantic-settings`, `pyjwt[crypto]`, `typer`, `uvicorn`; optional extras: `postgres` (`psycopg[binary,pool]>=3.3`), `documents` (`pypdf>=5`), `local-embeddings` (`onnxruntime`, `tokenizers`), `lancedb`. Dependency groups: `dev` (tests, linters, Office fixture libraries `python-docx`, `openpyxl`, `python-pptx`, `pillow`, `hypothesis`, `pip-audit`, pytest plugins), `bench`, `mutation`.
`cryptography` comes with `pyjwt[crypto]` (used for AES-GCM and HKDF); Starlette comes with `mcp`.

**REQ-TECH-04** MUST `[U2][U3]` **MCP protocol.** The official MCP Python SDK v2 (`mcp` 2.x, `MCPServer`) over Streamable HTTP, both protocol eras (REQ-AUTH-01), `stateless_http=True`, JSON responses, OAuth provider interface of the SDK (`OAuthAuthorizationServerProvider`, `TokenVerifier`), protected-resource metadata (RFC 9728).

**REQ-TECH-05** MUST `[U3][U4][D]` **Storage.** Embedded profile: SQLite (single serialised connection, WAL, FTS5 with `unicode61 remove_diacritics 0`, vectors as little-endian float32 blobs with exact scan, forward-only migrations by `user_version`) + filesystem blobs; external profile: PostgreSQL 16+ with pgvector >= 0.7 (`halfvec` HNSW, `tsvector`/GIN with the `simple` configuration, `text[]` tags, `jsonb`), blobs as rows. DuckDB is not used (rejected after analysis: single writer, experimental persistent HNSW).

**REQ-TECH-06** MUST `[U5][U34]` **Packaging.** Docker (single image; Annex K.1), Docker Compose for local convenience, Azure Container Apps via Bicep (Azure is the user's first cloud, ledger U4; GCP was left as a later port; the code stays cloud-neutral), GitHub Actions for CI and releases, GitHub repository `delian/hive-mcp` (the user named it as the place to pull, merge and push, ledger U8; the assistant advised keeping it private because the project holds company-knowledge tooling).

**REQ-TECH-07** SHOULD `[U3]` **Cloud neutrality.** The code depends on Docker, Postgres and files only; the Azure Blob adapter and GCP templates are **not built** (blobs are on the filesystem or in Postgres).

**REQ-TECH-08** MUST `[D]` **Configuration.** All runtime settings come from `HIVE_*` environment variables through one pydantic-settings class (`env_prefix=HIVE_`, unknown variables ignored), with validation at startup (Annex C); lists and maps are JSON in the variable; secrets are `SecretStr`; image defaults `HIVE_DATA_DIR=/data`, `HIVE_MODELS_DIR=/models`, `HIVE_PORT=8080`.

**REQ-TECH-09** MUST `[D]` **Web stack.** ASGI app built from the MCP SDK's Starlette app; uvicorn (with `proxy_headers` and `forwarded_allow_ips`); pure ASGI middleware; no JavaScript framework; no template engine (small escaped f-string pages).

**REQ-TECH-10** SHOULD `[D]` **Cryptography choices.** SHA-256 for token and file hashes; HMAC-SHA256 for cookies and Slack signatures; RS256 verification with PyJWT; AES-256-GCM with HKDF-SHA256 for sealed secrets; PKCE S256; `secrets.token_urlsafe` for tokens and ids (Annex E).

---

## 6. Explicit non-goals and accepted limitations

**NG-01** `[U2][D]` **No server-side LLM is required.** The server never summarises on its own; the calling agent writes summaries and merges. An optional model exists only for proposals (REQ-LLM-01).

**NG-02** `[D]` **A server cannot make an agent consult it** (MCP is pull-based). "Automatic" behaviour comes from three levers only: the server instructions and tool descriptions, client hooks (Claude Code), and an `AGENTS.md`/`CLAUDE.md` note.

**NG-03** `[U36][D]` **No IDE extensions** (VS Code/JetBrains plugin with its own UI); those editors reach the server through their MCP support, configured by `hive-hooks init`.

**NG-04** `[D]` **No automatic entity discovery, no entity schema.** Entity types are free text; agents propose entities.

**NG-05** `[D]` **No legacy Office formats** (`.doc .xls .ppt`), no encrypted PDFs, no OCR unless enabled.

**NG-06** `[U38]` **No embedded-Postgres watchdog** (the user declined it).

**NG-07** `[D]` **No multi-replica embedded storage** (SQLite and embedded Postgres are single-instance by design).

**NG-08** `[D]` **No Azure Blob/S3 blob adapter, no GCP templates, no cross-store blob sharing.** (Planned in the first design, not built.)

**NG-09** `[D]` **No `hive doctor` command, no import of knowledge bundles, no Tailscale-in-container, no terraform.** The first plan listed them; they were not built. (`hive migrate` and `hive backup` cover moving data; compose profiles cover tunnels; `hive export` (REQ-OPS-17) is a one-way analytics dump, not a bundle that can be imported.)

**NG-10** `[D]` **No per-scope hosted-embedding routing beyond the minimum-scope rule,** no per-question vote signal (the user chose to drop votes from ranking), no automatic merging of an other group's changes into a fork (the person reads `kb_diff_upstream` and decides).

**NG-11** `[D]` **No multi-tenant Teams bot, no Outlook support, no zero-install unfurling, no Slack posting beyond replies** (replies are private; channel posting only for mentions in a thread).

**NG-12** `[D]` **Retrieval quality figures are developer-set numbers.** Floors and weights were tuned on small hand-written sets; the user must evaluate on their own notes (`hive eval-build`, `hive eval --recommend`).

**NG-13** `[D]` **Not verified against real services** (see REQ-NFR-DOC-04); real-service trials are the next step.

**NG-14** `[D]` **Imported documents are not protected from the next sync.** A person's edit of a connector-imported item is replaced by the source on the next change of the document.

**NG-15** `[D]` **Erasure is not total.** Free-text audit details, a note's own text, expiring KV records and backups can still mention a person (documented).

**NG-16** `[D]` **No online search-index repair of Postgres HNSW after large deletions** (REINDEX is an operator action); Postgres keyword ranking is `ts_rank`, not BM25 (ParadeDB `pg_search` is the noted upgrade path).

**NG-17** `[D]` **Consent phishing is mitigated, not eliminated.** Open dynamic client registration is how assistants connect; `HIVE_OAUTH_REDIRECT_HOSTS` is the control and an edge rate limit is advised.

---

## 7. Open questions and assumptions needing confirmation

| ID | Type | Question or assumption | Where used |
|---|---|---|---|
| OQ-01 | [A] | Is a lost window of up to 15 minutes (default `HIVE_BACKUP_EVERY_MINUTES`) on a **hard crash** acceptable for the one-container embedded profile? The assistant asked; the user did not answer. If not, use the Postgres profile or lower the interval. **Decided (U45):** accepted; it is a documented trade-off of the single-container SQLite profile. | REQ-OPS-05 |
| OQ-02 | [A] | Real Google Workspace / Entra sign-in has never run; the OAuth flow was tested against a fake IdP with real signatures. A real tenant, OAuth client and public HTTPS URL are needed. | REQ-AUTH-05..08 |
| OQ-03 | [A] | Real claude.ai, ChatGPT and Gemini connector handshakes have not been tried (vendor-specific redirect URIs and DCR quirks; the first design said to verify against official docs). | REQ-AUTH-05, REQ-AUTH-13 |
| OQ-04 | [A] | Azure deployment (both Bicep templates) has never been deployed; Azure Files behaviour for backups and for SQLite fsync/locking is untested; Postgres sizing for 100 GB is unmeasured. | REQ-OPS-12 |
| OQ-05 | [A] | Slack (commands, shortcut, mentions) and Teams (bot, message extension, unfurling, dialog `card` wrapping) are verified only against signed fakes and Microsoft's documented shapes. | REQ-SLK, REQ-TMS |
| OQ-06 | [A] | Connectors for Confluence, Jira, Notion, SharePoint, Drive and GitHub were tested against mocked HTTP only; group sync against fakes only. | REQ-CONN, REQ-AUTH-14 |
| OQ-07 | [A] | Load/soak behaviour with hundreds of thousands of notes and many concurrent agents (SQLite and Postgres, several replicas), restore time on large databases and backup cost are unmeasured. | REQ-NFR-PERF-06 |
| OQ-08 | [A] | Claude Code accepting the generated `settings.json` hook entries and the `SessionStart`/`UserPromptSubmit` JSON output has been tested against a live server but never inside a real Claude Code session. | REQ-SESS-06 |
| OQ-09 | [A] | The user asked whether Slack/Teams could be "a configurable parameter" managed through the admin interface or an authenticated chat command (U37). The assistant read this as run-time admin-managed integration settings with secrets only on the admin page. Confirm that reading. | REQ-SLK-06 |
| OQ-10 | [A] | Which chat platform does the team really use, Google Workspace vs Microsoft 365 vs both, and is this for one team or for external customers (multi-tenant)? Asked; not answered. The design assumes one organisation per deployment, with org as the tenant boundary but not a hardened multi-customer SaaS. | REQ-ACC-01, section 6 |
| OQ-11 | [A] | `pg_dump`/`pg_restore` major version must be at least the server's; the image installs Debian's `postgresql-client`; `hive backup` uses whatever is on `PATH` (CI pins PG16). A mismatch fails with the tool's error. **Decided (U45):** no automatic client selection; `hive backup` and `hive restore` compare the client's major version with the server's before starting and stop with a message naming the version to install (`ops/pgclient.py`). | REQ-OPS-04 |
| OQ-12 | [A] | The relevance floors (bge 0.58, e5 0.81), the fusion weights and the rerank settings were tuned on developer-written sets. They are defaults until measured on real notes. | REQ-SRCH-04, REQ-EMB-03 |
| OQ-13 | [D] | Decided: export added. DuckDB stays dropped ("lite mode" means SQLite), but `hive export` writes JSONL or Parquet for analytics. | REQ-OPS-17, REQ-TECH-05 |
| OQ-14 | [A] | The first design promised fork/ignore/suggest-edit tools for foreign items; only "adopt by writing a new linked note" exists. **Decided (U45):** not enough; fork, diff-upstream, ignore and suggest-edit (with a review queue) were built, REQ-ACC-18. | REQ-ACC-15, REQ-ACC-18 |
| OQ-15 | [D] | Decided and built: bulk folder ingestion runs as resumable background jobs with progress, status and cancel (`hive ingest --background`, `ingest-status`, `ingest-cancel`). The 50 MiB cap stays for the synchronous path and for every non-text format (parsed whole in memory); plain-text files may reach 2 GiB in the background path, stored in pieces. | REQ-ING-15 |
| OQ-16 | [A] | Privacy/terms pages are plain defaults; the operator must supply an organisation name and contact and have the text reviewed before publishing the Teams app outside their own tenant. | REQ-ADM-08 |
| OQ-17 | [A] | The Teams app's `webApplicationInfo`/SSO and the bot's channel-install flow were not built; people are matched by email through the connector or pinned ids. | REQ-TMS-03 |

---

## 8. Traceability matrix

Paths are relative to `src/hive_mcp/` for modules and to `tests/` for tests. Where several requirements share a row, the row covers the range. Test directories are named when a whole directory exercises the requirement.

| Requirement(s) | Main modules | Main tests | Doc |
|---|---|---|---|
| REQ-KNO-01..08, 12, 15..17 | `domain/item.py`, `domain/tags.py`, `domain/rules.py`, `domain/scope.py`, `services/knowledge.py`, `adapters/{memory,sqlite,postgres}/items.py`, `mcp_api/tools.py`, `mcp_api/tools_browse.py` | `unit/test_item.py`, `unit/test_tags.py`, `conformance/test_items.py`, `services/test_knowledge.py` | STORAGE, SERVICES, MCP |
| REQ-KNO-09..11, 13..14 | `services/knowledge.py`, `services/sessions.py`, `services/curation.py` | `services/test_knowledge.py`, `services/test_curation.py` | CURATION |
| REQ-ACC-01..05 | `domain/access.py`, `domain/principal.py`, `domain/rules.py`, `security/principals.py`, adapters' `acl.py` | `unit/test_access.py`, `conformance/test_items.py`, `stress/test_access_load.py` | STORAGE |
| REQ-ACC-06..13, 16..17 | `domain/promotion.py`, `services/promotions.py`, `services/notifications.py`, `mcp_api/tools_promotion.py`, `adapters/*/promotions.py` | `services/test_promotions.py`, `conformance/test_promotions.py`, `stress/test_promotion_races.py`, `unit/test_approvers.py` | PROMOTION |
| REQ-ACC-18 | `services/foreign.py`, `services/foreign_records.py`, `mcp_api/tools_foreign.py`, `ports/kv.py` (`scan`) | `services/test_foreign.py`, `integration/test_foreign_tools.py`, `conformance/test_kv.py` | MCP |
| REQ-ACC-14..15 | `security/principals.py`, `mcp_api/tools.py`, `services/models.py` | `unit/test_tool_errors.py`, `integration/test_mcp_e2e.py` | MCP |
| REQ-SRCH-01..10, 13, 16 | `services/consult.py`, `services/consult_links.py`, `search/{ranking,tokenize,snippets}.py`, adapters' `text.py`/`vectors.py` | `unit/test_ranking.py`, `unit/test_snippets.py`, `conformance/test_text.py`, `conformance/test_vectors.py`, `services/test_consult_links.py` | EVALUATION, SERVICES |
| REQ-SRCH-11, 12, 14, 15 | `embeddings/rerank.py`, `evaluation/*`, `services/feedback.py`, `services/signals.py`, `cli_eval.py` | `services/test_rerank.py`, `unit/test_evaluation.py`, `unit/test_search_feedback.py`, `unit/test_rank_signals.py`, `ops/test_feedback_tools.py` | EVALUATION, LLM |
| REQ-EMB-01..10 | `embeddings/*`, `services/indexing.py`, `services/vector_admin.py`, `ingest/chunker.py`, `cli_maint.py` | `embeddings/test_onnx_and_models.py`, `embeddings/test_openai_compat.py`, `unit/test_hashing_embedder.py`, `unit/test_chunker.py`, `services/test_vector_admin.py` | EMBEDDINGS |
| REQ-EMB-11 | `adapters/lance/*` | `conformance/test_vectors_lance.py` | STORAGE |
| REQ-SESS-01..05, 15..17 | `services/sessions.py`, `services/session_sources.py`, `mcp_api/{instructions,prompts,resources,tools_hygiene}.py` | `services/test_sessions.py`, `unit/test_resource_render.py`, `ops/test_hygiene_tools.py` | AGENTS, MCP |
| REQ-SESS-06..14 | `clients/hooks/src/hive_hooks/*`, `scripts/install_client.sh`, `.github/workflows/release.yml` | `client/test_hooks.py`, `client/test_install.py`, `client/test_summary.py`, `client/test_init.py` | AGENTS |
| REQ-GRAPH-01..06 | `domain/edge.py`, `services/graph.py`, `adapters/*/edges.py`, `mcp_api/tools_graph.py` | `services/test_graph.py`, `conformance/test_edges.py` | GRAPH |
| REQ-ENT-01..06 | `domain/entity.py`, `services/entities.py`, `mcp_api/tools_entity.py` | `services/test_entities.py`, `unit/test_entity_slug.py` | ENTITIES |
| REQ-HIST-01..04 | `domain/history.py`, `adapters/*/history.py`, `mcp_api/tools_history.py`, `services/knowledge.py` | `services/test_item_history.py`, `conformance/test_history.py` | HISTORY |
| REQ-ING-01..11 | `ingest/*`, `services/ingest.py`, `cli_ingest.py`, `mcp_api/tools_ingest.py` | `services/test_ingest.py`, `services/test_ingest_url.py`, `unit/test_extract_and_sections.py`, `unit/test_office.py`, `unit/test_fetch.py`, `unit/test_ocr.py`, `integration/test_ingest_git.py`, `integration/test_cli_ingest.py`, `fuzz/` | INGEST, HARDENING |
| REQ-ING-15 | `ingest/walk.py`, `services/bulk_{ingest,files,state}.py`, `cli_ingest.py` | `unit/test_walk.py`, `services/test_bulk_ingest.py`, `integration/test_cli_ingest_background.py` | INGEST |
| REQ-ING-12..14 | `security/{redact,injection,sanitize}.py` | `unit/test_redact.py`, `unit/test_injection.py`, `fuzz/test_fuzz.py` | HARDENING |
| REQ-CUR-01..04 | `services/curation.py`, `mcp_api/tools_curation.py` | `services/test_curation.py` | CURATION |
| REQ-CUR-05..07, 09..10 | `services/duplicates.py`, `services/staleness.py`, `services/assist.py`, `mcp_api/tools_hygiene.py` | `services/test_duplicates.py`, `services/test_staleness.py`, `services/test_assist.py`, `ops/test_hygiene_tools.py` | CURATION, LLM |
| REQ-CUR-08, 11 | `services/digest.py`, `mcp_api/tools_digest.py`, `cli_digest.py`, `ops/loops.py` | `services/test_digest.py`, `ops/test_digest_tool.py` | DIGEST |
| REQ-RET-01..06 | `services/purge.py`, `services/retention.py`, `domain/retention.py`, `mcp_api/tools_purge.py`, `cli_ops.py`, `adapters/*/maintenance.py` | `services/test_purge.py`, `services/test_retention.py`, `unit/test_retention_rule.py`, `conformance/test_maintenance.py` | RETENTION |
| REQ-CONN-01..05 | `connectors/*`, `ops/loops.py`, `cli_connectors.py` | `connectors/*` | CONNECTORS |
| REQ-LLM-01..05 | `llm/*`, `services/assist.py`, `evaluation/assist_eval.py` | `services/test_assist.py`, `services/test_assist_eval.py` | LLM |
| REQ-AUTH-01..03 | `app.py`, `security/tokens.py`, `security/jsonfile.py`, `security/directory.py`, `cli.py` | `unit/test_tokens.py`, `unit/test_tokens_details.py`, `integration/test_protocols.py`, `integration/test_app.py` | MCP |
| REQ-AUTH-04..13, 15, 17 | `security/oauth/*`, `security/directory.py`, `security/google_auth.py`, `app_front.py` | `security/test_oauth_provider.py`, `security/test_token_service_details.py`, `security/test_revoke_details.py`, `security/test_login_units.py`, `integration/test_oauth_flow.py`, `unit/test_cookies_details.py` | OAUTH |
| REQ-AUTH-14 | `security/groups/*`, `cli_groups.py` | `security/test_group_sync.py` | GROUPS |
| REQ-ADM-01..09 | `webui/*`, `legal.py`, `mcp_api/tools_admin.py`, `services/admin.py`, `services/activity.py` | `webui/*`, `ops/test_legal.py`, `services/test_quota_admin.py`, `services/test_activity.py` | ADMIN |
| REQ-SLK-01..06 | `slack/*`, `integrations/*`, `mcp_api/tools_admin.py` | `slack/test_slack.py`, `unit/test_slack_verify.py`, `integrations/*` | SLACK |
| REQ-TMS-01..10 | `teams/*`, `cli_teams.py` | `teams/*` | TEAMS |
| REQ-NOT-01..03 | `notify/*`, `services/notifications.py` | `unit/test_notifiers.py`, `services/test_notifications.py`, `integrations/test_notify_chat.py` | PROMOTION |
| REQ-USE-01..05 | `services/quota.py`, `ops/ratelimit.py`, `adapters/postgres/ratelimit.py`, `ops/usage.py`, `services/activity.py` | `services/test_quota_admin.py`, `unit/test_ratelimit.py`, `stress/test_ratelimit_stress.py`, `services/test_activity.py` | HARDENING, ADMIN |
| REQ-OBS-01..05 | `ops/{logs,observe,metrics,health}.py` | `ops/test_observability.py` | OBSERVABILITY |
| REQ-OPS-01..03 | `ops/{serve,drain,state,loops}.py`, `services/runner.py`, adapters' `jobs.py` | `integration/test_drain.py`, `integration/test_sigterm.py`, `services/test_runner.py`, `conformance/test_jobs.py`, `stress/test_kv_and_jobs.py` | SERVICES |
| REQ-OPS-04..07 | `ops/{backup,autobackup,lease}.py` | `integration/test_backup.py`, `ops/test_autobackup.py`, `ops/test_lease.py`, `integration/test_single_container.py` | BACKUP, SINGLE-CONTAINER |
| REQ-OPS-08..09 | `ops/embedded_pg.py`, `ops/migrate/*`, `cli_migrate.py` | `ops/test_embedded_pg.py`, `ops/test_migrate.py` | MIGRATE, SINGLE-CONTAINER |
| REQ-OPS-10..16 | `Dockerfile`, `docker-compose.yml`, `deploy/*`, `cli_demo.py`, `scripts/*` | `ops/test_demo.py`, CI jobs `image`, `bicep` | DEPLOY, SINGLE-CONTAINER |
| REQ-OPS-17..18 | `export/*`, `cli_export.py`, `ports/{edges,maintenance}.py` (`list_page`, `ItemFilter`) | `conformance/test_export.py`, `conformance/test_edges.py`, `conformance/test_maintenance.py`, `ops/test_export_cli.py` | USAGE |
| REQ-OPS-14..15 | `adapters/postgres/*`, `adapters/sqlite/schema.py` | `adapters/test_replicas.py`, `adapters/test_postgres.py`, `adapters/test_sqlite.py` | STORAGE, DEPLOY |
| REQ-CI-01..10 | `.github/workflows/*`, `scripts/{check_file_size,mutation}.py/sh`, `pyproject.toml` | `unit/test_file_size_guard.py`, `fuzz/`, `stress/`, `unit/test_docs_drift.py` | HARDENING |
| REQ-MCP-01..05 | `mcp_api/*`, `app.py`, `bootstrap.py` | `unit/test_tool_errors.py`, `integration/test_mcp_e2e.py`, `integration/test_protocols.py`, `integration/test_app.py` | MCP |
| REQ-NFR-SEC-* | `security/*`, `ingest/fetch.py`, `ingest/office.py`, `ingest/ocr.py`, `webui/layout.py` | `hardening/test_review_fixes.py`, `security/*`, `fuzz/` | HARDENING |
| REQ-NFR-REL-*, REQ-NFR-PERF-* | `ops/*`, `adapters/*`, `bench/*` | `stress/*`, `integration/test_drain.py` | BENCHMARK, STORAGE |
| REQ-NFR-QA-*, REQ-NFR-DOC-* | repository-wide | `conformance/*`, `regression/*`, `unit/test_docs_drift.py` | PLAN, README |
| REQ-TECH-* | `pyproject.toml`, `uv.lock`, `config.py`, `config_extras.py` | `unit/test_docs_drift.py` | README, DEPLOY |
