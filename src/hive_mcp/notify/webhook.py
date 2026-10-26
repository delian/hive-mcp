"""Webhook notifier: POSTs JSON. `text` makes it work with Slack-style incoming webhooks as is."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from hive_mcp.domain.errors import NotifyError

if TYPE_CHECKING:
    from hive_mcp.ports.notify import Notification


class WebhookNotifier:
    def __init__(self, url: str, client: httpx.AsyncClient | None = None) -> None:
        self._url = url
        self._client = client or httpx.AsyncClient(timeout=10)

    async def send(self, notification: Notification) -> None:
        body = {
            "text": f"{notification.subject}\n{notification.text}",
            "event": notification.event,
            "subject": notification.subject,
            "message": notification.text,
        }
        try:
            resp = await self._client.post(self._url, json=body)
        except httpx.HTTPError as exc:
            raise NotifyError(f"webhook unreachable ({exc.__class__.__name__})") from exc
        if not resp.is_success:  # never put the URL in the message: it usually contains a secret
            raise NotifyError(f"webhook answered HTTP {resp.status_code}")
