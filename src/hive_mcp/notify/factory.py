"""Builds the configured notification channels."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.domain.errors import ValidationError
from hive_mcp.notify.smtp import SmtpNotifier
from hive_mcp.notify.webhook import WebhookNotifier

if TYPE_CHECKING:
    from hive_mcp.config import Settings
    from hive_mcp.ports.notify import Notifier


def make_channels(settings: Settings) -> dict[str, Notifier]:
    channels: dict[str, Notifier] = {}
    if settings.notify_webhook_url:
        channels["webhook"] = WebhookNotifier(settings.notify_webhook_url.get_secret_value())
    if settings.smtp_host:
        if not settings.smtp_from:
            raise ValidationError("HIVE_SMTP_FROM is required when HIVE_SMTP_HOST is set")
        channels["email"] = SmtpNotifier(
            settings.smtp_host,
            settings.smtp_port,
            settings.smtp_from,
            user=settings.smtp_user,
            password=settings.smtp_password.get_secret_value() if settings.smtp_password else None,
            starttls=settings.smtp_starttls,
        )
    return channels
