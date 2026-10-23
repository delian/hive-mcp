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
    allowed_origins: list[str] = Field(default_factory=list)
    drain_seconds: float = Field(default=10.0, ge=0)
