"""The settings of each chat integration, validated; secrets are kept apart from the rest."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from hive_mcp.domain.errors import ValidationError

SLACK, TEAMS = "slack", "teams"
NAMES = (SLACK, TEAMS)
SECRET_FIELDS = {SLACK: ("signing_secret", "bot_token"), TEAMS: ("app_password",)}
ID = re.compile(r"[A-Za-z0-9._:-]{1,80}")
# Teams conversation ids look like 19:abc...@thread.tacv2 (Slack channel ids are plain letters)
CHANNEL = re.compile(r"[A-Za-z0-9._:@-]{1,200}")
PROJECT = re.compile(r"[A-Za-z0-9._-]{1,64}")
MAX_MAP = 200


@dataclass(frozen=True, slots=True)
class IntegrationConfig:
    """One integration. `workspace` is the Slack workspace id or the Teams (Entra) tenant id;
    `app_id` is the Teams bot's application id. `channel_projects` maps a channel (Slack channel id,
    Teams conversation id) to the project that "Save to Hive" shares into for members of it."""

    name: str
    enabled: bool = False
    workspace: str = ""
    app_id: str = ""
    allow_mentions: bool = False  # answer when the bot is mentioned in a channel (in a thread)
    channel_projects: dict[str, str] = field(default_factory=dict)
    users: dict[str, str] = field(default_factory=dict)  # chat user id -> work email (no bot token)
    secrets: dict[str, str] = field(default_factory=dict)  # plaintext, in memory only

    def validate(self) -> None:
        if self.name not in NAMES:
            raise ValidationError(f"unknown integration {self.name!r}; use one of {NAMES}")
        for label, value in (("workspace", self.workspace), ("app_id", self.app_id)):
            if value and not ID.fullmatch(value):
                raise ValidationError(f"{label} may contain only letters, digits and . _ : -")
        if len(self.channel_projects) > MAX_MAP or len(self.users) > MAX_MAP:
            raise ValidationError(f"at most {MAX_MAP} channel and user entries")
        for channel, project in self.channel_projects.items():
            if not CHANNEL.fullmatch(channel) or not PROJECT.fullmatch(project):
                raise ValidationError(f"bad channel to project entry {channel!r}: {project!r}")
        for user, email in self.users.items():
            if not ID.fullmatch(user) or "@" not in email:
                raise ValidationError(f"bad user entry {user!r}: {email!r}")
        if self.enabled:
            self._require_complete()

    def _require_complete(self) -> None:
        if not self.workspace:
            raise ValidationError("a workspace (tenant) id is required to enable it")
        if self.name == SLACK:
            if not self.secrets.get("signing_secret"):
                raise ValidationError("the Slack signing secret is required to enable it")
            if not (self.secrets.get("bot_token") or self.users):
                raise ValidationError("a Slack bot token or a user map is required to enable it")
        elif not (self.app_id and self.secrets.get("app_password")):
            raise ValidationError("the Teams app id and app password are required to enable it")

    def public(self) -> dict[str, Any]:
        """Everything except the secrets (which are reported only as set or not set)."""
        return {
            "name": self.name, "enabled": self.enabled, "workspace": self.workspace,
            "app_id": self.app_id, "allow_mentions": self.allow_mentions,
            "channel_projects": dict(self.channel_projects), "users": dict(self.users),
            "secrets_set": sorted(k for k, v in self.secrets.items() if v),
        }  # fmt: skip

    def to_record(self) -> dict[str, Any]:
        record = self.public()
        record.pop("secrets_set")
        return record

    @staticmethod
    def from_record(
        name: str, record: dict[str, Any], secrets: dict[str, str]
    ) -> IntegrationConfig:
        return IntegrationConfig(
            name=name,
            enabled=bool(record.get("enabled", False)),
            workspace=str(record.get("workspace", "")),
            app_id=str(record.get("app_id", "")),
            allow_mentions=bool(record.get("allow_mentions", False)),
            channel_projects=dict(record.get("channel_projects", {})),
            users=dict(record.get("users", {})),
            secrets=secrets,
        )

    def with_changes(self, **changes: Any) -> IntegrationConfig:  # noqa: ANN401 - field updates
        return replace(self, **changes)
