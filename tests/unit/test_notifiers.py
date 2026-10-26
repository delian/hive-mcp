import smtplib
from typing import ClassVar

import httpx
import pytest

from hive_mcp.domain.errors import NotifyError
from hive_mcp.notify.smtp import SmtpNotifier
from hive_mcp.notify.webhook import WebhookNotifier
from hive_mcp.ports.notify import Notification

N = Notification("promotion.requested", "Subject line", "Body text", ("a@x.io", "b@x.io"))


async def test_webhook_posts_slack_compatible_json():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await WebhookNotifier("https://hooks.example/T/SECRET", client).send(N)
    body = seen[0].read()
    assert seen[0].url.path == "/T/SECRET" and b'"text"' in body and b"Subject line" in body
    assert b"a@x.io" not in body  # recipients are for email only


@pytest.mark.parametrize(
    "response", [httpx.Response(500), httpx.Response(404), httpx.ConnectError("no")]
)
async def test_webhook_failures_raise_without_leaking_the_url(response):
    def handler(request):
        if isinstance(response, Exception):
            raise response
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(NotifyError) as err:
        await WebhookNotifier("https://hooks.example/T/SECRET", client).send(N)
    assert "SECRET" not in str(err.value)


class FakeSmtp:
    log: ClassVar[list] = []

    def __init__(self, host, port, timeout):
        FakeSmtp.log.append(("connect", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context):
        FakeSmtp.log.append(("starttls",))

    def login(self, user, password):
        FakeSmtp.log.append(("login", user, password))

    def send_message(self, message):
        FakeSmtp.log.append(
            ("send", message["To"], message["Subject"], message.get_content().strip())
        )


async def test_email_goes_out_over_starttls_with_login(monkeypatch):
    FakeSmtp.log = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    await SmtpNotifier("mail.x.io", 587, "hive@x.io", user="u", password="p").send(N)
    assert FakeSmtp.log == [
        ("connect", "mail.x.io", 587),
        ("starttls",),
        ("login", "u", "p"),
        ("send", "a@x.io, b@x.io", "Subject line", "Body text"),
    ]


async def test_email_subject_cannot_inject_headers_and_empty_recipients_send_nothing(monkeypatch):
    FakeSmtp.log = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    evil = Notification("e", "Hi\nBcc: evil@x.io", "t", ("a@x.io",))
    await SmtpNotifier("m", 25, "h@x.io", starttls=False).send(evil)
    assert FakeSmtp.log[-1][2] == "Hi Bcc: evil@x.io"
    FakeSmtp.log = []
    await SmtpNotifier("m", 25, "h@x.io").send(Notification("e", "s", "t", ()))
    assert FakeSmtp.log == []


async def test_email_errors_become_notify_errors(monkeypatch):
    class Down(FakeSmtp):
        def __init__(self, *a, **k):
            raise ConnectionRefusedError

    monkeypatch.setattr(smtplib, "SMTP", Down)
    with pytest.raises(NotifyError):
        await SmtpNotifier("m", 25, "h@x.io").send(N)
