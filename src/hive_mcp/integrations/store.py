"""Reads and changes integration settings. Stored values beat the environment: the environment
(HIVE_SLACK_*) is the starting point until an admin saves settings here."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from hive_mcp.domain.errors import PermissionDenied, ValidationError
from hive_mcp.domain.principal import Role
from hive_mcp.integrations.config import NAMES, SECRET_FIELDS, SLACK, IntegrationConfig
from hive_mcp.ports.audit import AuditEvent

if TYPE_CHECKING:
    from collections.abc import Callable

    from hive_mcp.config import Settings
    from hive_mcp.domain.clock import Clock
    from hive_mcp.domain.principal import Principal
    from hive_mcp.integrations.secrets import SecretBox
    from hive_mcp.ports.audit import AuditLog
    from hive_mcp.ports.kv import KeyValueStore

log = logging.getLogger(__name__)
NS = "integration:config"
CACHE_SECONDS = 5.0
EDITABLE = ("enabled", "workspace", "app_id", "allow_mentions", "channel_projects", "users")


def env_defaults(settings: Settings) -> dict[str, IntegrationConfig]:
    """What the HIVE_SLACK_* variables say (Teams has none: it is configured here)."""
    secret = settings.slack_signing_secret
    token = settings.slack_bot_token
    slack = IntegrationConfig(
        SLACK,
        enabled=bool(secret),  # an empty HIVE_SLACK_SIGNING_SECRET= is not a secret
        workspace=settings.slack_team_id or "",
        users=dict(settings.slack_users),
        secrets={
            **({"signing_secret": secret.get_secret_value()} if secret else {}),
            **({"bot_token": token.get_secret_value()} if token else {}),
        },
    )
    return {SLACK: slack, "teams": IntegrationConfig("teams")}


class IntegrationStore:
    def __init__(  # noqa: PLR0913, PLR0917 - collaborators
        self,
        get_kv: Callable[[], KeyValueStore],
        get_audit: Callable[[], AuditLog],
        box: SecretBox,
        defaults: dict[str, IntegrationConfig],
        clock: Clock,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._get_kv, self._get_audit, self._box = get_kv, get_audit, box
        self._defaults, self._clock, self._now = defaults, clock, now
        self._cache: dict[str, tuple[float, IntegrationConfig]] = {}

    async def get(self, name: str) -> IntegrationConfig:
        if name not in NAMES:
            raise ValidationError(f"unknown integration {name!r}")
        cached = self._cache.get(name)
        if cached and self._now() - cached[0] < CACHE_SECONDS:
            return cached[1]
        record = await self._get_kv().get(NS, name)
        if record is None:
            config = self._defaults[name]
        else:
            sealed: dict[str, str] = record.get("sealed", {})
            try:
                secrets = {k: self._box.open(k, v) for k, v in sealed.items() if v}
                config = IntegrationConfig.from_record(name, record["config"], secrets)
            except ValidationError:
                # the secret key changed or the file is damaged: the integration is off until an
                # admin enters the secrets again; a broken setting must not break every request
                log.error("integration %s: stored secrets cannot be read; it is switched off", name)
                config = IntegrationConfig.from_record(name, record["config"], {})
                config = config.with_changes(enabled=False)
        self._cache[name] = (self._now(), config)
        return config

    async def update(
        self,
        actor: Principal,
        name: str,
        changes: dict[str, Any],
        secrets: dict[str, str] | None = None,
    ) -> IntegrationConfig:
        """Admins only. `changes` may hold only the editable fields; `secrets` maps a secret field
        to its new value ("" clears it). Nothing is stored if the result is not valid."""
        if actor.role < Role.ADMIN:
            raise PermissionDenied("only an admin can change integrations")
        current = await self.get(name)
        unknown = set(changes) - set(EDITABLE)
        bad_secrets = set(secrets or {}) - set(SECRET_FIELDS[name])
        if unknown or bad_secrets:
            raise ValidationError(f"unknown settings: {sorted(unknown | bad_secrets)}")
        merged = {**current.secrets, **{k: v for k, v in (secrets or {}).items()}}
        updated = current.with_changes(**changes, secrets={k: v for k, v in merged.items() if v})
        updated.validate()
        sealed = {k: self._box.seal(k, v) for k, v in updated.secrets.items()}
        await self._get_kv().put(NS, name, {"config": updated.to_record(), "sealed": sealed})
        self._cache.pop(name, None)
        touched = sorted([*changes, *(f"{k} (secret)" for k in (secrets or {}))])
        await self._get_audit().append(
            AuditEvent(
                actor.org_id, actor.id, "integration.update", self._clock.now(), name,
                {"changed": ", ".join(touched) or "nothing", "enabled": str(updated.enabled)},
            )
        )  # fmt: skip
        return updated
