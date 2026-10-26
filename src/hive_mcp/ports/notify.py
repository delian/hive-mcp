"""Outbound notifications (chat webhook, email). Delivery is at-least-once: sends may repeat."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Notification:
    event: str  # e.g. "promotion.requested"
    subject: str
    text: str
    recipients: tuple[str, ...] = ()  # email addresses; channels like a webhook ignore them


class Notifier(Protocol):
    async def send(self, notification: Notification) -> None:
        """Deliver or raise NotifyError (the job runner then retries with backoff)."""
