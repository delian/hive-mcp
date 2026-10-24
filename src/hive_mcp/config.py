"""Single source of truth for runtime configuration (env vars / `HIVE_*`)."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field
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
    embedder: str = "hashing"  # "hashing" (dev/test) until a real model adapter is configured
    run_jobs: bool = True  # run the background job runner inside the server process
    allowed_origins: list[str] = Field(default_factory=list)
    drain_seconds: float = Field(default=5.0, ge=0)  # readiness-fail window before shutdown
    shutdown_timeout: float = Field(default=30.0, ge=1)  # max wait for in-flight requests

    @property
    def tokens_path(self) -> Path:
        return self.tokens_file or self.data_dir / "tokens.json"

    @property
    def resource_url(self) -> str:
        return self.public_url.rstrip("/") + self.mcp_path
