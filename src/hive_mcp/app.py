"""ASGI application factory: authenticated MCP endpoint, health routes, background job runner."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings

from hive_mcp.app_front import (
    build_admin_ui,
    build_integrations,
    build_oauth,
    build_slack,
    build_teams,
)
from hive_mcp.bootstrap import Services, build_services
from hive_mcp.config import Settings
from hive_mcp.domain.errors import StorageBusy
from hive_mcp.legal import legal_routes
from hive_mcp.mcp_api.server import build_mcp
from hive_mcp.ops.health import health_routes
from hive_mcp.ops.loops import start_loops
from hive_mcp.ops.metrics import METRICS, metrics_routes
from hive_mcp.ops.observe import ObserveMiddleware
from hive_mcp.ops.ratelimit import Limiter, RateLimiter, RateLimitMiddleware, Rule
from hive_mcp.ops.state import Lifecycle
from hive_mcp.security.directory import UserDirectory
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.security.oauth.models import SCOPE
from hive_mcp.security.tokens import StaticTokenVerifier, TokenStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from mcp.server import MCPServer
    from mcp.server.auth.provider import TokenVerifier
    from starlette.applications import Starlette
    from starlette.routing import Route

    from hive_mcp.adapters.postgres.db import PgDatabase
    from hive_mcp.app_front import ServiceIntegrations
    from hive_mcp.security.oauth.provider import HiveOAuthProvider


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


log = logging.getLogger("hive.app")
BURST_SECONDS = 10  # a client may use this many seconds of its allowance at once


def rate_limit_rules(
    settings: Settings,
    make: Callable[[float, int], Limiter] | None = None,
    tokens: TokenStore | None = None,
) -> list[Rule]:
    """Registration is the only anonymous write, so it is the tightest; 0 turns a limit off."""
    make = make or (RateLimiter)
    rules = []
    if settings.rate_limit_register:
        per_hour = settings.rate_limit_register
        rules.append(
            Rule(("/register",), make(per_hour / 60, per_hour), methods=frozenset({"POST"}))
        )
    if settings.rate_limit_auth:
        per_min = settings.rate_limit_auth
        auth_paths = (
            "/authorize", "/token", "/revoke", "/oauth", "/admin/login", "/admin/google",
            "/slack", "/teams",
        )  # fmt: skip
        rules.append(Rule(auth_paths, make(per_min, max(per_min // 2, 5))))
    if settings.rate_limit_mcp:
        per_min = settings.rate_limit_mcp
        burst = max(per_min * BURST_SECONDS // 60, 10)
        own: dict[int, Limiter] = {}

        def per_key(key: str) -> Limiter | None:  # a token created with its own limit
            rate = tokens.rate_for_key(key) if tokens and key.startswith("tok:") else None
            if rate is None:
                return None
            return own.setdefault(rate, make(rate, max(rate * BURST_SECONDS // 60, 10)))

        rules.append(
            Rule((settings.mcp_path,), make(per_min, burst), by_token=True, per_key=per_key)
        )
    return rules


def shared_limiter(
    settings: Settings, get_services: Callable[[], Services]
) -> Callable[[float, int], Limiter] | None:
    """Buckets in Postgres when several replicas run; None keeps the in-process default."""
    if settings.replicas <= 1:
        return None
    from hive_mcp.adapters.postgres.ratelimit import PgRateLimiter  # noqa: PLC0415 - postgres extra

    def db() -> PgDatabase:
        return get_services().backend.db  # type: ignore[attr-defined,no-any-return]

    return lambda per_min, burst: PgRateLimiter(db, per_min, burst)


def front_doors(  # noqa: PLR0913, PLR0917 - everything the routes are built from
    settings: Settings,
    tokens: TokenStore,
    directory: UserDirectory,
    static: StaticTokenVerifier,
    integrations: ServiceIntegrations,
    get_services: Callable[[], Services],
    verifier: TokenVerifier | None,
) -> tuple[HiveOAuthProvider | None, list[Route]]:
    """Sign-in, admin pages and chat routes; the OAuth provider is None without Google/Entra."""
    provider: HiveOAuthProvider | None = None
    routes: list[Route] = []
    if settings.oauth_enabled and verifier is None:
        provider, routes = build_oauth(settings, static, lambda: get_services().backend.kv)
    if settings.admin_ui:
        routes += build_admin_ui(settings, tokens, get_services)
    routes += legal_routes(settings)
    routes += build_slack(directory, integrations, get_services)
    note_base = settings.public_url.rstrip("/") + "/admin/n/" if settings.admin_ui else None
    routes += build_teams(directory, integrations, get_services, note_base)
    return provider, routes


def create_app(
    settings: Settings | None = None,
    lifecycle: Lifecycle | None = None,
    *,
    services_factory: Callable[[Settings], Awaitable[Services]] = build_services,
    verifier: TokenVerifier | None = None,
    on_stop: Callable[[], None] | None = None,
) -> Starlette:
    """`on_stop` runs last when the server shuts down (after the final backup and the storage
    close). uvicorn re-raises SIGTERM once it has finished, so nothing after `run()` is reliable."""
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
        background = start_loops(services, settings)
        lifecycle.mark_ready()
        try:
            yield
        finally:
            lifecycle.start_draining()
            if runner:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runner
            for task in background:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            holder.pop("services", None)
            await services.close()
            if on_stop:
                on_stop()

    tokens = TokenStore(settings.tokens_path)
    directory = UserDirectory(AtomicJsonFile(settings.users_path, default=[]))
    static = StaticTokenVerifier(tokens, resource=settings.resource_url, directory=directory)
    integrations = build_integrations(get_services)
    provider, extra_routes = front_doors(
        settings, tokens, directory, static, integrations, get_services, verifier
    )
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
    scrape_token = settings.metrics_token.get_secret_value() if settings.metrics_token else None
    app.router.routes.extend(metrics_routes(scrape_token, METRICS, get_services))
    app.router.routes[0:0] = extra_routes  # ours first: /revoke must shadow the SDK's version
    app.add_middleware(
        RateLimitMiddleware,
        rules=rate_limit_rules(settings, shared_limiter(settings, get_services), tokens),
    )
    app.add_middleware(ObserveMiddleware, metrics=METRICS, mcp_path=settings.mcp_path)  # outermost
    app.state.lifecycle = lifecycle
    return app
