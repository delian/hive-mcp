"""The few HTML pages of the login flow. Everything dynamic is escaped; pages cannot be framed or cached."""

from __future__ import annotations

from html import escape

from starlette.responses import HTMLResponse

STYLE = (
    "body{font:16px system-ui,sans-serif;max-width:34rem;margin:3rem auto;padding:0 1rem;color:#1b1b1b}"
    "h1{font-size:1.3rem}.box{border:1px solid #bbb;border-radius:8px;padding:1rem;margin:1rem 0}"
    "button{font:inherit;padding:.6rem 1.2rem;margin-right:.6rem;border-radius:6px;border:1px solid #444;cursor:pointer}"
    "button.allow{background:#1b5e20;color:#fff;border-color:#1b5e20}code{word-break:break-all}"
)
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # No scripts at all; framing is forbidden (clickjacking on the consent button).
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
}


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    html = (
        f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>{escape(title)}</title><style>{STYLE}</style><body><h1>{escape(title)}</h1>{body}</body></html>"
    )
    return HTMLResponse(html, status_code=status, headers=SECURITY_HEADERS)


def error_page(message: str, status: int = 400) -> HTMLResponse:
    return page(
        "Sign-in problem",
        f"<p>{escape(message)}</p><p>Close this window and start again from the app.</p>",
        status,
    )


def consent_page(  # noqa: PLR0913 - everything the user needs to make an informed choice
    *, app_name: str, redirect_host: str, email: str, scopes: list[str], txn: str, csrf: str
) -> HTMLResponse:
    body = (
        f"<p>An application wants to use the team knowledge base <b>as {escape(email)}</b>.</p>"
        f"<div class=box><p>Application name (chosen by the app, not verified): <b>{escape(app_name)}</b></p>"
        f"<p>After you decide, you will be sent to: <code>{escape(redirect_host)}</code></p>"
        f"<p>It will be able to: {escape(', '.join(scopes))} (read and write what you can access)</p></div>"
        "<p>Only continue if you started this from an application you trust.</p>"
        f"<form method=post action='/oauth/consent'><input type=hidden name=txn value='{escape(txn, quote=True)}'>"
        f"<input type=hidden name=csrf value='{escape(csrf, quote=True)}'>"
        "<button class=allow name=decision value=approve>Allow</button>"
        "<button name=decision value=deny>Deny</button></form>"
    )
    return page("Allow access?", body)
