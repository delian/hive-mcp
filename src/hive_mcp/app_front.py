"""The front doors of the server: sign-in (Google, Entra, OIDC), the admin pages and Slack."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.domain.clock import SystemClock
from hive_mcp.integrations.secrets import SecretBox
from hive_mcp.integrations.store import IntegrationStore, env_defaults
from hive_mcp.security.directory import UserDirectory
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.security.oauth.cookies import CookieSigner, load_secret_key
from hive_mcp.security.oauth.models import OAuthConfig
from hive_mcp.security.oauth.provider import HiveOAuthProvider
from hive_mcp.security.oauth.routes import oauth_routes
from hive_mcp.security.oauth.upstream import GoogleOidc
from hive_mcp.slack.handlers import SlackBot
from hive_mcp.slack.identity import SlackIdentity
from hive_mcp.slack.routes import SlackRoutes
from hive_mcp.webui.account_routes import AccountPages
from hive_mcp.webui.accounts import AccountAdmin
from hive_mcp.webui.auth import AdminAuth
from hive_mcp.webui.ops_pages import OpsPages
from hive_mcp.webui.routes import AdminUi
from hive_mcp.webui.session import SessionCodec, SessionStore
from hive_mcp.webui.signin import SignIn

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.routing import Route

    from hive_mcp.bootstrap import Services
    from hive_mcp.config import Settings
    from hive_mcp.integrations.config import IntegrationConfig
    from hive_mcp.ports.kv import KeyValueStore
    from hive_mcp.security.tokens import StaticTokenVerifier, TokenStore


def make_upstream(settings: Settings, callback_url: str) -> GoogleOidc:
    """The configured identity provider (Google, Entra ID or another OIDC provider)."""
    assert settings.google_client_id and settings.google_client_secret  # noqa: S101 - guarded by oauth_enabled
    return GoogleOidc(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret.get_secret_value(),
        issuer=settings.google_issuer,
        callback_url=callback_url,
        provider=settings.sso_provider,
        tenant=settings.entra_tenant,
    )


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
    upstream = make_upstream(settings, f"{settings.public_url.rstrip('/')}/oauth/google/callback")
    key = settings.secret_key.get_secret_value() if settings.secret_key else None
    signer = CookieSigner(load_secret_key(key, settings.data_dir / "secret.key"))
    return provider, oauth_routes(provider, upstream, signer, settings.google_allowed_domains)


def build_integrations(
    settings: Settings, get_services: Callable[[], Services]
) -> IntegrationStore:
    """Run-time settings of the chat integrations, starting from the environment."""
    key = settings.secret_key.get_secret_value() if settings.secret_key else None
    box = SecretBox(load_secret_key(key, settings.data_dir / "secret.key"))
    return IntegrationStore(
        lambda: get_services().backend.kv,
        lambda: get_services().backend.audit,
        box,
        env_defaults(settings),
        SystemClock(),
    )


def build_slack(
    directory: UserDirectory,
    store: IntegrationStore,
    get_services: Callable[[], Services],
) -> list[Route]:
    """The Slack routes; the stored integration settings decide whether and how they answer."""

    def kv() -> KeyValueStore:
        return get_services().backend.kv

    def make_bot(config: IntegrationConfig) -> SlackBot:
        identity = SlackIdentity(
            directory,
            kv,
            team_id=config.workspace,
            bot_token=config.secrets.get("bot_token"),
            pinned=config.users,
        )
        return SlackBot(
            get_services,
            identity,
            channel_projects=config.channel_projects,
            bot_token=config.secrets.get("bot_token"),
        )

    return SlackRoutes(lambda: store.get("slack"), make_bot, kv).routes()


def build_admin_ui(
    settings: Settings, tokens: TokenStore, get_services: Callable[[], Services]
) -> list[Route]:
    directory = UserDirectory(AtomicJsonFile(settings.users_path, default=[]))
    key = settings.secret_key.get_secret_value() if settings.secret_key else None
    secret = load_secret_key(key, settings.data_dir / "secret.key")
    upstream = None
    if settings.oauth_enabled:
        assert settings.google_client_id and settings.google_client_secret  # noqa: S101
        upstream = make_upstream(
            settings, f"{settings.public_url.rstrip('/')}/admin/google/callback"
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
    ui.ops = OpsPages(ui, get_services, settings)
    return ui.routes()
