"""Calling a tool on the server from a hook: a short deadline, failures left to the caller."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

TOKEN_FILE = Path.home() / ".config" / "hive" / "token"
DEFAULT_TIMEOUT = 5.0


class NotConfigured(Exception):
    """No server address or token: hooks stay silent instead of erroring in every session."""


def settings_from_env() -> tuple[str, str]:
    url = os.environ.get("HIVE_URL", "").rstrip("/")
    token = os.environ.get("HIVE_TOKEN", "").strip()
    if not token and TOKEN_FILE.is_file():
        token = TOKEN_FILE.read_text().strip()
    if not url or not token:
        raise NotConfigured("set HIVE_URL (e.g. https://kb.example.com/mcp) and HIVE_TOKEN")
    return url, token


async def call_tool(
    name: str, arguments: dict[str, Any], deadline: float = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    """The tool's structured result. Raises on any failure (network, auth, tool error, timeout)."""
    url, token = settings_from_env()
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=deadline)
    async with asyncio.timeout(deadline):
        async with Client(streamable_http_client(url, http_client=http)) as client:
            result = await client.call_tool(name, arguments)
    if result.is_error:
        raise RuntimeError(f"{name} failed")
    return dict(result.structured_content or {})
