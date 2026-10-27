"""Settings of the optional parts: backups, a language model, ranking nudges, retention rules,
connectors and Slack. Kept apart so `config.py` stays small; `Settings` inherits them."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - pydantic reads the annotations at runtime
from typing import Any, Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExtrasSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HIVE_", extra="ignore")

    # One-container durability (docs/SINGLE-CONTAINER.md): write verified backup archives here every
    # few minutes (if anything changed) and restore the newest one when started on an empty disk.
    backup_dir: Path | None = None
    backup_every_minutes: int = Field(default=15, ge=1, le=1440)
    backup_keep: int = Field(default=8, ge=1, le=1000)
    # Seconds a starting instance waits for a running one to release the backup directory.
    backup_lease_wait: float = Field(default=90.0, ge=0)
    # An optional language model for merge proposals and contradiction checks (docs/LLM.md). Notes
    # narrower than llm_min_scope (default: project) are never sent to it.
    llm_provider: Literal["none", "openai", "anthropic"] = "none"
    llm_url: str | None = None  # openai: the chat completions URL; anthropic: optional override
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_azure: bool = False  # openai: send the key as an `api-key` header (Azure OpenAI)
    llm_min_scope: str = "project"
    # Small bounded ranking nudges from votes (kb_rate) and age; off by default.
    rank_signals: bool = False
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

    @model_validator(mode="after")
    def _check_retention_rules(self) -> Self:
        from hive_mcp.domain.errors import ValidationError  # noqa: PLC0415
        from hive_mcp.domain.retention import parse_rules  # noqa: PLC0415

        try:
            parse_rules(self.retention_rules)
        except ValidationError as exc:
            raise ValueError(f"HIVE_RETENTION_RULES: {exc}") from exc
        return self

    @model_validator(mode="after")
    def _check_connectors(self) -> Self:
        from hive_mcp.connectors.spec import parse_specs  # noqa: PLC0415
        from hive_mcp.domain.errors import ValidationError  # noqa: PLC0415

        try:
            parse_specs(self.connectors)
        except ValidationError as exc:
            raise ValueError(f"HIVE_CONNECTORS: {exc}") from exc
        return self

    @model_validator(mode="after")
    def _check_slack(self) -> Self:
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
