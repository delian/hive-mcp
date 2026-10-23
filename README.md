# hive-mcp

A lightweight Model Context Protocol (MCP) server for sharing knowledge with a
team. It provides tools to save, search, list, and delete entries in a SQLite
database. The server uses only the Python standard library.

## Run

Requires Python 3.9 or newer. Configure your MCP client to launch the server
over stdio:

```json
{
  "mcpServers": {
    "hive": {
      "command": "python3",
      "args": ["/absolute/path/to/hive-mcp/server.py"],
      "env": {
        "HIVE_MCP_DB": "/shared/team-storage/hive-knowledge.sqlite3"
      }
    }
  }
}
```

`HIVE_MCP_DB` is optional; by default, the database is stored at
`~/.hive-mcp/knowledge.sqlite3`. To share entries across teammates, configure
each server to use the same database path on storage that is accessible to
everyone. SQLite is intended for a shared local or mounted filesystem, not
simultaneous access over a network filesystem that does not support SQLite
locking.

## Tools

- `save_knowledge`: save a required `title` and `content`, with optional
  string-array `tags` and `author`. Returns the entry and its generated ID.
- `search_knowledge`: search title, content, and tags case-insensitively with
  required `query` and optional `limit` (1–100, default 20).
- `list_knowledge`: list newest entries, with optional `limit` (1–100,
  default 20) and `offset`.
- `delete_knowledge`: delete an entry by its ID.

## Test

```sh
python3 -m unittest discover -s tests
```
