# hive-hooks

A small client for machines where coding agents run. It installs Claude Code hooks and an `AGENTS.md` note that make the
agent consult and report to a hive-mcp knowledge base. It needs only `mcp` and `typer`, none of the server's dependencies.

```bash
uv tool install hive-hooks            # or: uvx --from hive-hooks hive-hooks ...
export HIVE_URL=https://kb.example.com/mcp HIVE_TOKEN=hive_...
hive-hooks hooks install              # add --scope project to share through git
hive-hooks hooks status
```
See `docs/AGENTS.md` in the server repository for what each hook does, privacy, and the optional summarizer.
