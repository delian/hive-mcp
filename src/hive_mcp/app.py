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

from hive_mcp.bootstrap import Services, build_services
from hive_mcp.config import Settings
from hive_mcp.domain.clock import SystemClock
from hive_mcp.domain.errors import StorageBusy
from hive_mcp.mcp_api.server import build_mcp
from hive_mcp.ops.health import health_routes
from hive_mcp.ops.loops import digest_loop, group_sync_loop, purge_loop, usage_loop
from hive_mcp.ops.metrics import METRICS, metrics_routes
from hive_mcp.ops.observe import ObserveMiddleware
from hive_mcp.ops.ratelimit import Limiter, RateLimiter, RateLimitMiddleware, Rule
from hive_mcp.ops.state import Lifecycle
from hive_mcp.security.directory import UserDirectory
from hive_mcp.security.groups.factory import group_sync_configured
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.security.oauth.cookies import CookieSigner, load_secret_key
from hive_mcp.security.oauth.models import SCOPE, OAuthConfig
from hive_mcp.security.oauth.provider import HiveOAuthProvider
from hive_mcp.security.oauth.routes import oauth_routes
from hive_mcp.security.oauth.upstream import GoogleOidc
from hive_mcp.security.tokens import StaticTokenVerifier, TokenStore
from hive_mcp.webui.account_routes import AccountPages
from hive_mcp.webui.accounts import AccountAdmin
from hive_mcp.webui.auth import AdminAuth
from hive_mcp.webui.routes import AdminUi
from hive_mcp.webui.session import SessionCodec, SessionStore
from hive_mcp.webui.signin import SignIn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from mcp.server import MCPServer
    from mcp.server.auth.provider import TokenVerifier
    from starlette.applications import Starlette
    from starlette.routing import Route

    from hive_mcp.adapters.postgres.db import PgDatabase
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
        auth_paths = ("/authorize", "/token", "/revoke", "/oauth", "/admin/login", "/admin/google")
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
        settings.consent_remember_days * 86400,
        frozenset(h.lower() for h in settings.oauth_redirect_hosts),
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


def build_admin_ui(
    settings: Settings, tokens: TokenStore, get_services: Callable[[], Services]
) -> list[Route]:
    directory = UserDirectory(AtomicJsonFile(settings.users_path, default=[]))
    key = settings.secret_key.get_secret_value() if settings.secret_key else None
    secret = load_secret_key(key, settings.data_dir / "secret.key")
    upstream = None
    if settings.oauth_enabled:
        assert settings.google_client_id and settings.google_client_secret  # noqa: S101
        upstream = GoogleOidc(
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret.get_secret_value(),
            issuer=settings.google_issuer,
            callback_url=f"{settings.public_url.rstrip('/')}/admin/google/callback",
        )
    signin = SignIn(
        get_services,
        AdminAuth(directory, tokens),
        SessionStore(SessionCodec(secret), lambda: get_services().backend.kv),
        CookieSigner(secret),
        public_url=settings.public_url,
        upstream=upstream,
        allowed_domains=settings.google_allowed_domains,
        directory=directory,
    )
    quota = (settings.quota_items_per_user, settings.quota_chars_per_user)
    ui = AdminUi(get_services, signin, quota)
    sessions = signin.sessions
    ui.accounts = AccountPages(
        ui, AccountAdmin(directory, tokens, sessions, lambda: get_services().backend.audit)
    )
    return ui.routes()


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
        maintenance = asyncio.create_task(purge_loop(services, settings.retention_session_days))
        metering = asyncio.create_task(usage_loop(services))
        digesting = (
            asyncio.create_task(
                digest_loop(services, settings.digest_weekday, settings.digest_hour_utc)
            )
            if services.digest_sender and settings.digest_weekday is not None
            else None
        )
        syncing = (
            asyncio.create_task(group_sync_loop(settings))
            if settings.group_sync_minutes and group_sync_configured(settings)
            else None
        )
        lifecycle.mark_ready()
        try:
            yield
        finally:
            lifecycle.start_draining()
            if runner:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runner
            for task in (maintenance, syncing, digesting, metering):
                if task:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            holder.pop("services", None)
            await services.close()

    tokens = TokenStore(settings.tokens_path)
    directory = UserDirectory(AtomicJsonFile(settings.users_path, default=[]))
    static = StaticTokenVerifier(tokens, resource=settings.resource_url, directory=directory)
    extra_routes: list[Route] = []
    provider: HiveOAuthProvider | None = None
    if settings.oauth_enabled and verifier is None:
        provider, extra_routes = build_oauth(settings, static, lambda: get_services().backend.kv)
    if settings.admin_ui:
        extra_routes += build_admin_ui(settings, tokens, get_services)
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
