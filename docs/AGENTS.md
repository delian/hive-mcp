# Making coding agents use the knowledge base

Agents use the knowledge base when something prompts them: the server `instructions`, an `AGENTS.md` note, and (for
Claude Code) hooks that run at fixed moments. Hooks do not depend on the model remembering.

## Claude Code
On the machine where the agent runs (needs the `hive` CLI: `uv tool install hive-mcp`):
```bash
export HIVE_URL=https://kb.example.com/mcp      # the server's /mcp address
export HIVE_TOKEN=hive_...                       # a token for you: `hive token create` on the server, or a file at ~/.config/hive/token
claude mcp add --transport http hive "$HIVE_URL" --header "Authorization: Bearer $HIVE_TOKEN"
hive hooks install                               # --scope project to share via git, --no-prompt-hook to skip per-prompt search
hive hooks status                                # shows where hooks are installed and whether the server answers
```
| Hook | What it does |
|---|---|
| `SessionStart` | tells the agent the knowledge base exists and lists what it holds about this project (top 5) |
| `UserPromptSubmit` | for a request of 6+ words (not a `/command`), searches and adds up to 3 relevant items; silent when nothing relevant exists |
| `PreCompact` | stores the conversation as a compaction report (`kb_report_compaction`) so it can be recovered |
| `SessionEnd` | stores a session report (`kb_report_session`): a short mechanical summary plus the conversation text |

Reports are **private to you** and secrets in them are masked on the server. They contain your conversation text, so
decide whether you want that stored; remove the `PreCompact` and `SessionEnd` entries from the settings file if not. A report is
idempotent (the same session and content is never stored twice); the summary is mechanical (first request, last reply,
counts), because no model runs in a hook. Ask the agent to call `kb_report_session` itself for a real summary.

**Hooks fail open.** They have short deadlines, never print errors into the session, and do nothing when `HIVE_URL`/`HIVE_TOKEN`
are missing or the server is down. Set `HIVE_DEBUG=1` to see why a hook stayed silent (on stderr).

`hive hooks install` merges into `~/.claude/settings.json` (or the project/local file), keeps your other hooks and
settings, can be run again safely, keeps the original once as `settings.json.hive-backup`, and refuses to touch a file it
cannot parse. `hive hooks remove` takes exactly our entries out again. `--command "uvx --from hive-mcp hive"` changes how
Claude Code starts the CLI when it is not on its `PATH`. `--dry-run` shows the result without writing.

## Other agents (Codex, Gemini CLI, Cursor, ...)
They have no hook system that fits, so give them the instructions note:
```bash
hive hooks snippet --file AGENTS.md      # or CLAUDE.md / GEMINI.md; --remove takes it out again
```
The note sits between `<!-- hive-mcp:begin -->` and `<!-- hive-mcp:end -->`; running the command again updates it in place.
Add the server as an MCP server in the agent's own configuration.
