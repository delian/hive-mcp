"""ASGI application factory: authenticated MCP endpoint, health routes, background job runner."""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings

from hive_mcp.bootstrap import Services, build_services
from hive_mcp.config import Settings
from hive_mcp.domain.errors import StorageBusy
from hive_mcp.mcp_api.server import build_mcp
from hive_mcp.ops.health import health_routes
from hive_mcp.ops.state import Lifecycle
from hive_mcp.security.tokens import StaticTokenVerifier, TokenStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from mcp.server import MCPServer
    from mcp.server.auth.provider import TokenVerifier
    from starlette.applications import Starlette


def transport_security(settings: Settings) -> TransportSecuritySettings:
    """Host and Origin allow-lists (DNS-rebinding protection): the public host plus loopback."""
    public = urlparse(settings.public_url)
    hosts = {"localhost:*", "127.0.0.1:*", "[::1]:*", public.netloc, public.hostname or ""}
    origins = {f"{public.scheme}://{public.netloc}", *settings.allowed_origins}
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(h for h in hosts if h),
        allowed_origins=sorted(origins),
    )


def create_app(
    settings: Settings | None = None,
    lifecycle: Lifecycle | None = None,
    *,
    services_factory: Callable[[Settings], Awaitable[Services]] = build_services,
    verifier: TokenVerifier | None = None,
) -> Starlette:
    settings = settings or Settings()
    lifecycle = lifecycle or Lifecycle()
    holder: dict[str, Services] = {}

    def get_services() -> Services:
        if "services" not in holder:
            raise StorageBusy("starting up, retry in a few seconds")
        return holder["services"]

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        services = await services_factory(settings)
        holder["services"] = services
        runner = asyncio.create_task(services.runner.run_forever()) if settings.run_jobs else None
        lifecycle.mark_ready()
        try:
            yield
        finally:
            lifecycle.start_draining()
            if runner:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runner
            holder.pop("services", None)
            await services.close()

    auth = AuthSettings(
        issuer_url=settings.public_url,
        resource_server_url=settings.resource_url,
        validate_token_resource=False,  # the verifier below checks tokens itself
    )
    mcp = build_mcp(
        get_services,
        token_verifier=verifier or StaticTokenVerifier(TokenStore(settings.tokens_path)),
        auth=auth,
        lifespan=lifespan,
    )
    app = mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        stateless_http=True,
        json_response=True,
        host=settings.host,
        transport_security=transport_security(settings),
    )
    app.router.routes.extend(health_routes(lifecycle))
    app.state.lifecycle = lifecycle
    return app
