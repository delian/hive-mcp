"""Email notifier over SMTP (STARTTLS by default). Blocking smtplib runs in a worker thread."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import NotifyError

if TYPE_CHECKING:
    from hive_mcp.ports.notify import Notification


class SmtpNotifier:
    def __init__(  # noqa: PLR0913 - connection settings
        self,
        host: str,
        port: int,
        sender: str,
        *,
        user: str | None = None,
        password: str | None = None,
        starttls: bool = True,
        timeout: float = 15.0,
    ) -> None:
        self._host, self._port, self._sender = host, port, sender
        self._user, self._password = user, password
        self._starttls, self._timeout = starttls, timeout

    def _deliver(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
            if self._starttls:
                smtp.starttls(context=ssl.create_default_context())
            if self._user and self._password:
                smtp.login(self._user, self._password)
            smtp.send_message(message)

    async def send(self, notification: Notification) -> None:
        if not notification.recipients:
            return
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = ", ".join(notification.recipients)
        # One line, no header injection: split() also folds \r, \v, \f, U+0085, U+2028/9, which
        # make EmailMessage raise (an item title may contain any of them).
        message["Subject"] = " ".join(notification.subject.split())[:200]
        message.set_content(notification.text)
        try:
            await asyncio.to_thread(self._deliver, message)
        except (OSError, smtplib.SMTPException) as exc:
            raise NotifyError(f"email not sent ({exc.__class__.__name__})") from exc
