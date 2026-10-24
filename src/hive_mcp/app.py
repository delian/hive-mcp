"""ASGI application factory: authenticated MCP endpoint, health routes, background job runner."""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings

from hive_mcp.bootstrap import Services, build_services
from hive_mcp.config import Settings
from hive_mcp.domain.clock import SystemClock
from hive_mcp.domain.errors import StorageBusy
from hive_mcp.mcp_api.server import build_mcp
from hive_mcp.ops.health import health_routes
from hive_mcp.ops.state import Lifecycle
from hive_mcp.security.directory import UserDirectory
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.security.oauth.cookies import CookieSigner, load_secret_key
from hive_mcp.security.oauth.models import SCOPE, OAuthConfig
from hive_mcp.security.oauth.provider import HiveOAuthProvider
from hive_mcp.security.oauth.routes import oauth_routes
from hive_mcp.security.oauth.upstream import GoogleOidc
from hive_mcp.security.tokens import StaticTokenVerifier, TokenStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from mcp.server import MCPServer
    from mcp.server.auth.provider import TokenVerifier
    from starlette.applications import Starlette
    from starlette.routing import Route

    from hive_mcp.ports.kv import KeyValueStore


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


PURGE_INTERVAL_SECONDS = 600


async def purge_loop(services: Services) -> None:
    """Expired records are already invisible; this just reclaims their space."""
    while True:
        await asyncio.sleep(PURGE_INTERVAL_SECONDS)
        with contextlib.suppress(StorageBusy):
            await services.backend.kv.purge_expired()


def build_oauth(
    settings: Settings, static: StaticTokenVerifier, get_kv: Callable[[], KeyValueStore]
) -> tuple[HiveOAuthProvider, list[Route]]:
    """Google-backed authorization server; static tokens keep working next to OAuth tokens."""
    assert settings.google_client_id and settings.google_client_secret  # noqa: S101 - guarded by oauth_enabled
    config = OAuthConfig(
        settings.public_url,
        settings.resource_url,
        settings.access_token_ttl,
        settings.refresh_token_ttl,
    )
    provider = HiveOAuthProvider(
        get_kv=get_kv,
        directory=UserDirectory(AtomicJsonFile(settings.users_path, default=[])),
        config=config,
        clock=SystemClock(),
        static_tokens=static,
    )
    upstream = GoogleOidc(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret.get_secret_value(),
        issuer=settings.google_issuer,
        callback_url=f"{settings.public_url.rstrip('/')}/oauth/google/callback",
    )
    key = settings.secret_key.get_secret_value() if settings.secret_key else None
    signer = CookieSigner(load_secret_key(key, settings.data_dir / "secret.key"))
    return provider, oauth_routes(provider, upstream, signer, settings.google_allowed_domains)


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
        maintenance = asyncio.create_task(purge_loop(services))
        lifecycle.mark_ready()
        try:
            yield
        finally:
            lifecycle.start_draining()
            if runner:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runner
            maintenance.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await maintenance
            holder.pop("services", None)
            await services.close()

    static = StaticTokenVerifier(TokenStore(settings.tokens_path), resource=settings.resource_url)
    extra_routes: list[Route] = []
    provider: HiveOAuthProvider | None = None
    if settings.oauth_enabled and verifier is None:
        provider, extra_routes = build_oauth(settings, static, lambda: get_services().backend.kv)
    auth = AuthSettings(
        issuer_url=settings.public_url,
        resource_server_url=settings.resource_url,
        validate_token_resource=True,
        required_scopes=[SCOPE],
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        )
        if provider
        else None,
        revocation_options=RevocationOptions(enabled=True) if provider else None,
    )
    mcp = build_mcp(
        get_services,
        token_verifier=None if provider else (verifier or static),
        auth_server_provider=provider,
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
    app.router.routes[0:0] = extra_routes  # ours first: /revoke must shadow the SDK's version
    app.state.lifecycle = lifecycle
    return app
