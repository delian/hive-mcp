import json
import tempfile
import unittest
from pathlib import Path

from server import KnowledgeStore, MCPServer


class MCPServerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        store = KnowledgeStore(Path(self.temp_dir.name) / "knowledge.sqlite3")
        self.server = MCPServer(store)

    def tearDown(self):
        self.temp_dir.cleanup()

    def call(self, name, arguments, request_id=1):
        return self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )["result"]

    def test_initialize_and_tool_discovery(self):
        initialized = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        )
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "hive-mcp")
        tools = self.server.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )
        self.assertEqual(
            {tool["name"] for tool in tools["result"]["tools"]},
            {"save_knowledge", "search_knowledge", "list_knowledge", "delete_knowledge"},
        )

    def test_save_search_list_and_delete(self):
        saved = json.loads(
            self.call(
                "save_knowledge",
                {
                    "title": "Release process",
                    "content": "Deploy with the release checklist.",
                    "tags": ["operations"],
                    "author": "Alex",
                },
            )["content"][0]["text"]
        )
        search = json.loads(
            self.call("search_knowledge", {"query": "CHECKLIST"})["content"][0]["text"]
        )
        self.assertEqual(search[0]["id"], saved["id"])
        self.assertEqual(search[0]["tags"], ["operations"])

        listed = json.loads(
            self.call("list_knowledge", {"limit": 1})["content"][0]["text"]
        )
        self.assertEqual(listed[0]["title"], "Release process")

        deleted = json.loads(
            self.call("delete_knowledge", {"id": saved["id"]})["content"][0]["text"]
        )
        self.assertTrue(deleted["deleted"])
        self.assertEqual(
            json.loads(
                self.call("search_knowledge", {"query": "release"})["content"][0]["text"]
            ),
            [],
        )

    def test_search_treats_wildcards_as_literal_text(self):
        self.call(
            "save_knowledge",
            {"title": "Wildcard", "content": "100% complete"},
        )
        self.assertEqual(
            json.loads(
                self.call("search_knowledge", {"query": "%"})["content"][0]["text"]
            )[0]["title"],
            "Wildcard",
        )
        self.assertEqual(
            json.loads(
                self.call("search_knowledge", {"query": "missing%"})["content"][0]["text"]
            ),
            [],
        )

    def test_entries_are_visible_to_another_server_using_the_same_database(self):
        self.call(
            "save_knowledge",
            {"title": "Shared", "content": "Visible to teammates"},
        )
        other_server = MCPServer(
            KnowledgeStore(Path(self.temp_dir.name) / "knowledge.sqlite3")
        )
        response = other_server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "search_knowledge",
                    "arguments": {"query": "teammates"},
                },
            }
        )
        entries = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(entries[0]["title"], "Shared")

    def test_invalid_tool_input_is_reported_as_tool_error(self):
        result = self.call("save_knowledge", {"title": " "})
        self.assertTrue(result["isError"])
        self.assertIn("title", result["content"][0]["text"])

    def test_notifications_do_not_produce_responses(self):
        self.assertIsNone(
            self.server.handle(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
        )

    def test_rejects_invalid_json_rpc_request(self):
        response = self.server.handle({"method": "ping"})
        self.assertEqual(response["error"]["code"], -32600)


if __name__ == "__main__":
    unittest.main()
