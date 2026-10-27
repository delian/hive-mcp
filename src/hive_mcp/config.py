"""Single source of truth for runtime configuration (env vars / `HIVE_*`)."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Profile(StrEnum):
    """Storage profile; same image, different adapters."""

    EMBEDDED = "embedded"
    EXTERNAL = "external"


MIN_SECRET_KEY = 32  # signs login and admin cookies: short keys can be brute-forced offline


class Settings(BaseSettings):
    """Process settings. Secrets come from env vars, never from the image."""

    model_config = SettingsConfigDict(env_prefix="HIVE_", extra="ignore")

    profile: Profile = Profile.EMBEDDED
    host: str = "0.0.0.0"  # noqa: S104 - container default; bind narrowly outside Docker
    port: int = Field(default=8080, ge=1, le=65535)
    data_dir: Path = Path("/data")
    mcp_path: str = "/mcp"
    public_url: str = (
        "http://localhost:8080"  # externally visible base URL (tunnel / load balancer)
    )
    database_url: str | None = None  # required for the external profile (Postgres + pgvector)
    tokens_file: Path | None = None  # default: <data_dir>/tokens.json
    embedder: str = "hashing"  # "hashing" (dev/test only), "onnx" (offline) or "openai" (hosted)
    reranker: str = (
        "none"  # "onnx": reorder the best results with a cross-encoder (see docs/EVALUATION.md)
    )
    rerank_model: str = "ms-marco-minilm-l6-int8"
    rerank_depth: int = Field(default=15, ge=1, le=50)
    # "lancedb": keep vectors in a LanceDB directory instead of SQLite (embedded profile, extra).
    vector_store: Literal["default", "lancedb"] = "default"
    embedding_model: str = "bge-small-en-v1.5-int8"  # onnx: pinned model name; openai: model id
    models_dir: Path | None = None  # onnx model files; default: <data_dir>/models
    embedding_url: str | None = None  # openai: full .../embeddings URL
    embedding_api_key: SecretStr | None = None
    embedding_dim: int = Field(default=1024, ge=8, le=4096)  # openai: requested dimensions
    # Notifications about promotion requests/decisions (all optional; item content is never sent).
    notify_webhook_url: SecretStr | None = None  # Slack-style incoming webhook or any JSON endpoint
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    smtp_starttls: bool = True
    # Narrowest scope whose items may be embedded (private, project, team, org). Default: all for
    # local embedders; project and wider for hosted ones, so private notes never leave the server.
    embedding_min_scope: str | None = None
    embedding_azure: bool = False  # openai: send `api-key` header (Azure OpenAI)
    # Overrides the embedder's relevance floor; `hive eval --recommend` suggests one for your data.
    relevance_floor: float | None = Field(default=None, ge=0, le=1)
    embedding_min_similarity: float = Field(default=0.3, ge=0, le=1)  # openai relevance floor
    quota_items_per_user: int = Field(default=0, ge=0)  # 0 = unlimited; admins are exempt
    quota_chars_per_user: int = Field(
        default=0, ge=0
    )  # characters of title + body, archived excluded
    retention_session_days: int = Field(
        default=0, ge=0
    )  # purge session/compaction logs older than this; 0 = keep
    # One-container durability (docs/SINGLE-CONTAINER.md): write verified backup archives here every
    # few minutes (if anything changed) and restore the newest one when started on an empty disk.
    backup_dir: Path | None = None
    backup_every_minutes: int = Field(default=15, ge=1, le=1440)
    backup_keep: int = Field(default=8, ge=1, le=1000)
    # Seconds a starting instance waits for a running one to release the backup directory.
    backup_lease_wait: float = Field(default=90.0, ge=0)
    # Delete-by-age rules: [{"kind": "session", "days": 90}, {"scope": "private", "days": 730}].
    # Each needs a kind or a scope; age counts from the last change. See docs/RETENTION.md.
    retention_rules: list[dict[str, Any]] = Field(default_factory=list)
    # Scheduled imports from URLs, GitHub or Google Drive, run in this process (docs/CONNECTORS.md).
    connectors: list[dict[str, Any]] = Field(default_factory=list)
    # Slack: the /hive command and a "Save to Hive" message shortcut (docs/SLACK.md). Needs the
    # app's signing secret and the workspace id; people are matched by their Slack email through
    # the bot token (users:read.email), or by the fixed map {"U123": "ann@acme.com"} without one.
    slack_signing_secret: SecretStr | None = None
    slack_team_id: str | None = None
    slack_bot_token: SecretStr | None = None
    slack_users: dict[str, str] = Field(default_factory=dict)
    # Fetching documents from URLs on users' behalf (kb_ingest_url): off unless enabled.
    ingest_urls_enabled: bool = False
    ingest_url_hosts: list[str] = Field(default_factory=list)  # empty = any public host
    ingest_url_allow_private: bool = False  # let the server reach private/internal addresses
    # Teams, projects, approvers from Google Workspace groups (docs/GROUPS.md); off without the map.
    group_map_file: Path | None = None
    group_sync_key_file: Path | None = None  # service account key (domain-wide delegation)
    group_sync_admin: str | None = None  # Workspace admin the service account acts as
    group_sync_minutes: int = Field(default=0, ge=0)  # 0 = only when `hive sync-groups` is run
    metrics_token: SecretStr | None = (
        None  # /metrics (Prometheus) is served only with a scrape token
    )
    log_format: Literal["text", "json"] = "text"
    log_level: str = "INFO"
    # Record searches and the results opened afterwards in the audit trail (admins only) so
    # `hive eval-build` can make an evaluation set from real use. Off by default (shows topics).
    search_feedback: bool = False
    # Weekly email digest (needs SMTP): day of the week, 0 = Monday ... 6 = Sunday; unset = off.
    digest_weekday: int | None = Field(default=None, ge=0, le=6)
    digest_hour_utc: int = Field(default=8, ge=0, le=23)
    # "Still true?" section in the weekly digest for notes older than this many days; 0 = off.
    stale_review_days: int = Field(default=0, ge=0, le=3650)
    admin_ui: bool = False  # serve the small web admin pages under /admin (opt-in)
    # Number of server processes sharing one database (external profile). Above 1: rate limits are
    # shared through Postgres, quota reads are fresh, and a fixed HIVE_SECRET_KEY is required.
    replicas: int = Field(default=1, ge=1)
    # Read scanned PDFs and images with Tesseract (needs tesseract + poppler-utils; docs/INGEST.md).
    ocr: bool = False
    ocr_languages: str = "eng"  # Tesseract codes joined with +, e.g. eng+deu
    ocr_max_pages: int = Field(default=50, ge=1, le=500)  # pages OCR may read in one document
    run_jobs: bool = True  # run the background job runner inside the server process
    # Interactive sign-in (claude.ai, ChatGPT, Gemini) via Google Workspace; needs id and secret.
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_issuer: str = "https://accounts.google.com"
    # Which identity provider the client id/secret above belong to: "google", "entra" (Microsoft
    # Entra ID; needs HIVE_ENTRA_TENANT) or "oidc" (any provider; HIVE_GOOGLE_ISSUER is its issuer).
    sso_provider: Literal["google", "entra", "oidc"] = "google"
    entra_tenant: str | None = None  # the directory (tenant) id, a GUID
    google_allowed_domains: list[str] = Field(
        default_factory=list
    )  # e.g. ["acme.com"]; empty = refuse all
    # Hosts that may be OAuth redirect targets (claude.ai, chatgpt.com...); empty = any, which
    # leaves the consent page as the only defence against a look-alike app.
    oauth_redirect_hosts: list[str] = Field(default_factory=list)
    users_file: Path | None = None  # default: <data_dir>/users.json (see `hive user`)
    secret_key: SecretStr | None = (
        None  # signs login cookies; default: generated into <data_dir>/secret.key
    )
    # Requests per minute per client address (login/registration) or per token (MCP); 0 = unlimited.
    rate_limit_mcp: int = Field(default=1200, ge=0)
    rate_limit_auth: int = Field(default=60, ge=0)
    rate_limit_register: int = Field(
        default=30, ge=0
    )  # new client registrations per hour per address
    forwarded_allow_ips: str = (
        "127.0.0.1"  # proxies whose X-Forwarded-For is trusted for client addresses
    )
    consent_remember_days: int = Field(default=30, ge=0)  # 0 = show the approval page every time
    access_token_ttl: int = Field(default=3600, ge=60)
    refresh_token_ttl: int = Field(default=30 * 86400, ge=3600)
    allowed_origins: list[str] = Field(default_factory=list)
    drain_seconds: float = Field(default=5.0, ge=0)  # readiness-fail window before shutdown
    shutdown_timeout: float = Field(default=30.0, ge=1)  # max wait for in-flight requests

    @model_validator(mode="after")
    def _check_secret_key(self) -> Settings:
        if self.secret_key is not None and len(self.secret_key.get_secret_value()) < MIN_SECRET_KEY:
            raise ValueError(
                f"HIVE_SECRET_KEY needs {MIN_SECRET_KEY}+ characters (openssl rand -hex 32)"
            )
        return self

    @model_validator(mode="after")
    def _check_retention_rules(self) -> Settings:
        from hive_mcp.domain.errors import ValidationError  # noqa: PLC0415
        from hive_mcp.domain.retention import parse_rules  # noqa: PLC0415

        try:
            parse_rules(self.retention_rules)
        except ValidationError as exc:
            raise ValueError(f"HIVE_RETENTION_RULES: {exc}") from exc
        return self

    @model_validator(mode="after")
    def _check_connectors(self) -> Settings:
        from hive_mcp.connectors.spec import parse_specs  # noqa: PLC0415
        from hive_mcp.domain.errors import ValidationError  # noqa: PLC0415

        try:
            parse_specs(self.connectors)
        except ValidationError as exc:
            raise ValueError(f"HIVE_CONNECTORS: {exc}") from exc
        return self

    @model_validator(mode="after")
    def _check_slack(self) -> Settings:
        if self.slack_signing_secret is None:
            return self
        if not self.slack_team_id:
            raise ValueError(
                "HIVE_SLACK_SIGNING_SECRET needs HIVE_SLACK_TEAM_ID (the workspace id)"
            )
        if not (self.slack_bot_token or self.slack_users):
            raise ValueError(
                "Slack needs HIVE_SLACK_BOT_TOKEN or HIVE_SLACK_USERS to know who asks"
            )
        return self

    @model_validator(mode="after")
    def _check_sso(self) -> Settings:
        if self.sso_provider == "entra" and not self.entra_tenant:
            raise ValueError("HIVE_SSO_PROVIDER=entra needs HIVE_ENTRA_TENANT (the directory id)")
        return self

    @model_validator(mode="after")
    def _check_replicas(self) -> Settings:
        if self.replicas > 1 and self.profile is Profile.EMBEDDED:
            raise ValueError(
                "HIVE_REPLICAS > 1 needs the external profile (SQLite is single-process)"
            )
        if self.replicas > 1 and self.secret_key is None:
            raise ValueError("HIVE_REPLICAS > 1 needs HIVE_SECRET_KEY so all replicas sign alike")
        return self

    @property
    def models_path(self) -> Path:
        return self.models_dir or self.data_dir / "models"

    @property
    def tokens_path(self) -> Path:
        return self.tokens_file or self.data_dir / "tokens.json"

    @property
    def users_path(self) -> Path:
        return self.users_file or self.data_dir / "users.json"

    @property
    def oauth_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def resource_url(self) -> str:
        return self.public_url.rstrip("/") + self.mcp_path
