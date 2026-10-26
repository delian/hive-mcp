"""Builds the MCP server: instructions, tools and prompts over a lazily available service stack."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp.server import MCPServer

from hive_mcp import __version__
from hive_mcp.mcp_api.instructions import INSTRUCTIONS
from hive_mcp.mcp_api.prompts import register_prompts
from hive_mcp.mcp_api.resources import register_resources
from hive_mcp.mcp_api.tools import register_tools
from hive_mcp.mcp_api.tools_curation import register_curation_tools
from hive_mcp.mcp_api.tools_graph import register_graph_tools
from hive_mcp.mcp_api.tools_ingest import register_ingest_tools
from hive_mcp.mcp_api.tools_promotion import register_promotion_tools

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager

    from mcp.server.auth.provider import OAuthAuthorizationServerProvider, TokenVerifier
    from mcp.server.auth.settings import AuthSettings

    from hive_mcp.bootstrap import Services


def build_mcp(
    get_services: Callable[[], Services],
    *,
    token_verifier: TokenVerifier | None = None,
    auth_server_provider: OAuthAuthorizationServerProvider[Any, Any, Any] | None = None,
    auth: AuthSettings | None = None,
    lifespan: Callable[[MCPServer], AbstractAsyncContextManager[AsyncIterator[None] | None]]
    | None = None,
) -> MCPServer:
    mcp = MCPServer(
        "hive-mcp",
        instructions=INSTRUCTIONS,
        version=__version__,
        token_verifier=token_verifier,
        auth_server_provider=auth_server_provider,
        auth=auth,
        lifespan=lifespan,
    )
    register_tools(mcp, get_services)
    register_promotion_tools(mcp, get_services)
    register_graph_tools(mcp, get_services)
    register_ingest_tools(mcp, get_services)
    register_curation_tools(mcp, get_services)
    register_prompts(mcp)
    register_resources(mcp, get_services)

    @mcp.prompt()
    def consult_before_task(task: str) -> str:
        """Start a task by checking what the team already knows."""
        return f"Before starting, call kb_consult for: {task}\nThen proceed, citing the origin of anything you reuse."

    @mcp.prompt()
    def end_of_session_report() -> str:
        """Wrap up a session: store reusable findings and the session record."""
        return (
            "Review this session. For each reusable finding call kb_remember (one idea per item, with tags). "
            "Then call kb_report_session with a short summary, the outcomes and (optionally) the log."
        )

    return mcp
