"""Allow `python -m hive_mcp` to start the server."""

from hive_mcp.cli import cli

cli(["serve"])
