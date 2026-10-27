# Making coding agents use the knowledge base

Agents use the knowledge base when something prompts them: the server `instructions`, an `AGENTS.md` note, and (for
Claude Code) hooks that run at fixed moments. Hooks do not depend on the model remembering.

## Claude Code
On the machine where the agent runs. The client is a separate small package, **`hive-hooks`** (38 packages, 46 MB, against
100 MB for the server): it needs only `mcp` and `typer`. It is not on PyPI yet; install it from the repository:
```bash
uv tool install "hive-hooks @ git+https://github.com/delian/hive-mcp#subdirectory=clients/hooks"   # gives the command `hive-hooks`
```
Everything below works the same with `hive-hooks ...` in place of `hive ...` (a full server install also has the same
commands under `hive`). `install` writes the command name you started it with into the hook entries.
```bash
export HIVE_URL=https://kb.example.com/mcp      # the server's /mcp address
export HIVE_TOKEN=hive_...                       # a token for you: `hive token create` on the server, or a file at ~/.config/hive/token
claude mcp add --transport http hive "$HIVE_URL" --header "Authorization: Bearer $HIVE_TOKEN"
hive-hooks hooks install                         # --scope project to share via git, --no-prompt-hook to skip per-prompt search
hive-hooks hooks status                          # shows where hooks are installed and whether the server answers
```
### One command
`hive-hooks init` does the whole setup and never writes the token into a project file:
```bash
export HIVE_TOKEN=hive_...
hive-hooks init --url https://kb.example.com/mcp --editor claude,vscode,cursor --agents-file AGENTS.md --save-token
```
It installs the hooks (`--scope user|project|local`, or `--no-hooks`), merges a `hive` server into `.mcp.json`
(Claude Code), `.vscode/mcp.json` (VS Code, which asks for the token once) and `.cursor/mcp.json` (Cursor) in the current
project (`--root` to change), puts the instructions note between markers in `--agents-file`, optionally stores the token in
`~/.config/hive/token` with owner-only permissions (`--save-token`), and finally tries the server. Other servers in those
files stay; running it again changes nothing. On a fresh machine `scripts/install_client.sh --url ...` installs the client
with `uv` first and then runs the same command. Tagged releases (`v*`) attach wheels for the server and the client to the
GitHub release; publishing to PyPI is opt-in (see `.github/workflows/release.yml`):
1. On pypi.org create the projects `hive-mcp` and `hive-hooks` as *pending publishers* (Account, Publishing): owner
   `delian`, repository `hive-mcp`, workflow `release.yml`, environment `pypi`. No token is stored anywhere.
2. In the GitHub repository create an environment named `pypi` (optionally with required reviewers) and the repository
   variable `PUBLISH_TO_PYPI=true`.
3. Raise `version` in `pyproject.toml` and `clients/hooks/pyproject.toml` to the same number, then
   `git tag v0.1.0 && git push origin v0.1.0`. The workflow refuses a tag that differs from the files.
After the first release, `uv tool install hive-hooks` works and `scripts/install_client.sh` can use
`HIVE_CLIENT_SOURCE=hive-hooks`.

IDE extensions (a VS Code or JetBrains plugin with its own UI) are not built: those editors reach the knowledge base through
their MCP support, which `init` configures.

| Hook | What it does |
|---|---|
| `SessionStart` | tells the agent the knowledge base exists and lists what it holds about this project (top 5) |
| `UserPromptSubmit` | for a request of 6+ words (not a `/command`), searches and adds up to 3 relevant items; silent when nothing relevant exists |
| `PreCompact` | stores the conversation as a compaction report (`kb_report_compaction`) so it can be recovered |
| `SessionEnd` | stores a session report (`kb_report_session`): a short mechanical summary plus the conversation text |

Reports are **private to you** and secrets in them are masked on the server. They contain your conversation text, so
decide whether you want that stored; remove the `PreCompact` and `SessionEnd` entries from the settings file if not. A report is
idempotent (the same session and content is never stored twice).

**What the summary contains.** No model runs in a hook, so the summary is extracted: the first request, up to five of the
agent's most informative sentences (changes, decisions, causes, numbers; chatter and code are skipped; in the order they
happened), the files it edited and the messages of the commits it made (also stored as the report's *outcomes*). If you have a
model at hand, plug it in with `HIVE_HOOK_SUMMARIZER`, a command that receives the conversation on stdin and prints a summary:

```bash
export HIVE_HOOK_SUMMARIZER='claude -p "Summarise this coding session for a team knowledge base in under 120 words: what was asked, what was decided and why, what changed."'
export HIVE_HOOK_SUMMARIZER_TIMEOUT=20      # seconds (max 25: Claude Code stops the hook at its own timeout, 30 s)
```

The command is run directly (no shell) with the last 30,000 characters on stdin; if it fails, prints nothing, or is too slow
it is killed and the extracted summary is used. The conversation text goes to whatever that command talks to: choose it
accordingly.

**Hooks fail open.** They have short deadlines, never print errors into the session, and do nothing when `HIVE_URL`/`HIVE_TOKEN`
are missing or the server is down. Set `HIVE_DEBUG=1` to see why a hook stayed silent (on stderr).

`hive hooks install` merges into `~/.claude/settings.json` (or the project/local file), keeps your other hooks and
settings, can be run again safely, keeps the original once as `settings.json.hive-backup`, and refuses to touch a file it
cannot parse. `hive hooks remove` takes exactly our entries out again. `--command "uvx --from hive-hooks hive-hooks"` changes how
Claude Code starts the CLI when it is not on its `PATH`. `--dry-run` shows the result without writing.

## Other agents (Codex, Gemini CLI, Cursor, ...)
They have no hook system that fits, so give them the instructions note:
```bash
hive-hooks hooks snippet --file AGENTS.md      # or CLAUDE.md / GEMINI.md; --remove takes it out again
```
The note sits between `<!-- hive-mcp:begin -->` and `<!-- hive-mcp:end -->`; running the command again updates it in place.
Add the server as an MCP server in the agent's own configuration.

## What did the agent rely on? (`session` labels)

Pass one label (4-80 characters, your choice) to `kb_consult`, `kb_get` and `kb_report_session` for a piece of work. The
server remembers, for a day and per person, which notes were **shown** and which were **opened** (ids only), and
`kb_session_sources(session)` lists them, opened first, with their origin, author and last change, so a reviewer can see
what a result was built on. `kb_report_session` also links the stored session note `derived_from` each opened note (up
to 20), which keeps the trail in the knowledge graph (`kb_graph`) after the label expires. A label belongs to the person
who used it; nobody else can read it. The Claude Code hooks do this automatically with the label `cc-<session id>` and
tell the agent to pass it.
