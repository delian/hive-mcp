#!/usr/bin/env python3
"""MCP server for sharing team knowledge through a shared SQLite database."""

import json
import os
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "save_knowledge",
        "description": "Save a knowledge entry for the team.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "A short title."},
                "content": {"type": "string", "description": "The knowledge to share."},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for categorization.",
                },
                "author": {
                    "type": "string",
                    "description": "Optional name of the contributor.",
                },
            },
            "required": ["title", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_knowledge",
        "description": "Search team knowledge by words in its title, content, or tags.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to search for."},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Maximum results to return (default 20).",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_knowledge",
        "description": "List recent team knowledge entries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Maximum results to return (default 20).",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Number of entries to skip.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_knowledge",
        "description": "Delete a team knowledge entry by its ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Entry ID."}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
]


class KnowledgeStore:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    author TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _entry(row):
        entry = dict(row)
        entry["tags"] = json.loads(entry["tags"])
        return entry

    def save(self, title, content, tags=None, author=""):
        entry = {
            "id": str(uuid.uuid4()),
            "title": title.strip(),
            "content": content.strip(),
            "tags": json.dumps(tags or [], ensure_ascii=False),
            "author": author.strip(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if not entry["title"] or not entry["content"]:
            raise ValueError("title and content must not be empty")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO knowledge (id, title, content, tags, author, created_at)
                VALUES (:id, :title, :content, :tags, :author, :created_at)
                """,
                entry,
            )
        return self._entry(entry)

    def search(self, query, limit=20):
        query = query.strip()
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM knowledge
                WHERE title LIKE ? ESCAPE '\\' COLLATE NOCASE
                   OR content LIKE ? ESCAPE '\\' COLLATE NOCASE
                   OR tags LIKE ? ESCAPE '\\' COLLATE NOCASE
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (pattern, pattern, pattern, limit),
            ).fetchall()
        return [self._entry(row) for row in rows]

    def list_recent(self, limit=20, offset=0):
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._entry(row) for row in rows]

    def delete(self, entry_id):
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM knowledge WHERE id = ?", (entry_id,))
        return cursor.rowcount > 0


class MCPServer:
    def __init__(self, store):
        self.store = store

    def _call_tool(self, name, arguments):
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        if name == "save_knowledge":
            self._check_fields(arguments, {"title", "content", "tags", "author"})
            title = self._required_string(arguments, "title")
            content = self._required_string(arguments, "content")
            tags = arguments.get("tags", [])
            if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
                raise ValueError("tags must be an array of strings")
            author = arguments.get("author", "")
            if not isinstance(author, str):
                raise ValueError("author must be a string")
            return self.store.save(title, content, tags, author)
        if name == "search_knowledge":
            self._check_fields(arguments, {"query", "limit"})
            query = self._required_string(arguments, "query")
            limit = self._limit(arguments.get("limit", 20))
            return self.store.search(query, limit)
        if name == "list_knowledge":
            self._check_fields(arguments, {"limit", "offset"})
            limit = self._limit(arguments.get("limit", 20))
            offset = arguments.get("offset", 0)
            if type(offset) is not int or offset < 0:
                raise ValueError("offset must be a non-negative integer")
            return self.store.list_recent(limit, offset)
        if name == "delete_knowledge":
            self._check_fields(arguments, {"id"})
            entry_id = self._required_string(arguments, "id")
            if not self.store.delete(entry_id):
                raise ValueError("knowledge entry not found")
            return {"deleted": True, "id": entry_id}
        raise ValueError(f"unknown tool: {name}")

    @staticmethod
    def _check_fields(arguments, allowed):
        unexpected = set(arguments) - allowed
        if unexpected:
            raise ValueError(f"unexpected argument: {sorted(unexpected)[0]}")

    @staticmethod
    def _required_string(arguments, field):
        value = arguments.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        return value

    @staticmethod
    def _limit(value):
        if type(value) is not int or not 1 <= value <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        return value

    def handle(self, message):
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
        ):
            return self._error(None, -32600, "Invalid Request")
        method = message.get("method")
        request_id = message.get("id")
        if method.startswith("notifications/"):
            return None
        if "id" not in message:
            return None
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "Invalid params")
        try:
            if method == "initialize":
                requested_version = params.get("protocolVersion", PROTOCOL_VERSION)
                result = {
                    "protocolVersion": requested_version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "hive-mcp", "version": "1.0.0"},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                name = params.get("name")
                if not isinstance(name, str):
                    return self._error(request_id, -32602, "Tool name is required")
                try:
                    value = self._call_tool(name, params.get("arguments", {}))
                except (ValueError, TypeError) as error:
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [{"type": "text", "text": str(error)}],
                            "isError": True,
                        },
                    }
                result = {
                    "content": [
                        {"type": "text", "text": json.dumps(value, ensure_ascii=False)}
                    ],
                    "isError": False,
                }
            else:
                return self._error(request_id, -32601, "Method not found")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (AttributeError, TypeError):
            return self._error(request_id, -32602, "Invalid params")

    @staticmethod
    def _error(request_id, code, message):
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


def main():
    database_path = os.environ.get(
        "HIVE_MCP_DB", "~/.hive-mcp/knowledge.sqlite3"
    )
    server = MCPServer(KnowledgeStore(database_path))
    for line in sys.stdin:
        try:
            message = json.loads(line)
            response = server.handle(message)
        except json.JSONDecodeError:
            response = MCPServer._error(None, -32700, "Parse error")
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
