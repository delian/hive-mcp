"""Single source of truth for runtime configuration (env vars / `HIVE_*`)."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Profile(StrEnum):
    """Storage profile; same image, different adapters."""

    EMBEDDED = "embedded"
    EXTERNAL = "external"


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
    embedding_min_similarity: float = Field(default=0.3, ge=0, le=1)  # openai relevance floor
    quota_items_per_user: int = Field(default=0, ge=0)  # 0 = unlimited; admins are exempt
    quota_chars_per_user: int = Field(
        default=0, ge=0
    )  # characters of title + body, archived excluded
    retention_session_days: int = Field(
        default=0, ge=0
    )  # purge session/compaction logs older than this; 0 = keep
    run_jobs: bool = True  # run the background job runner inside the server process
    # Interactive sign-in (claude.ai, ChatGPT, Gemini) via Google Workspace; needs id and secret.
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_issuer: str = "https://accounts.google.com"
    google_allowed_domains: list[str] = Field(
        default_factory=list
    )  # e.g. ["acme.com"]; empty = refuse all
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
