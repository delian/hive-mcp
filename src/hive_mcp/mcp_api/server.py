"""Builds the MCP server object. Tools are registered from one place."""

from __future__ import annotations

from mcp.server import MCPServer

from hive_mcp import __version__

INSTRUCTIONS = (
    "Shared team knowledge base. Before starting a task, search it for prior work. "
    "After finishing research or learning something reusable, store it. "
    "Results from other groups are labelled with their origin."
)


def build_mcp() -> MCPServer:
    mcp = MCPServer("hive-mcp", instructions=INSTRUCTIONS, version=__version__)

    @mcp.tool()
    def ping(message: str = "pong") -> str:
        """Health check tool: echoes the message."""
        return message

    return mcp
