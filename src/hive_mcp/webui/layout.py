"""Page shell for the admin UI. Everything dynamic goes through `esc`; no scripts, no framing."""

from __future__ import annotations

from html import escape as esc

from starlette.responses import HTMLResponse, RedirectResponse

from hive_mcp.security.oauth.pages import SECURITY_HEADERS

STYLE = (
    "body{font:15px system-ui,sans-serif;max-width:64rem;margin:1.5rem auto;padding:0 1rem;color:#1b1b1b}"
    "nav{display:flex;gap:1rem;align-items:center;border-bottom:1px solid #ccc;padding-bottom:.6rem}"
    "nav form{margin-left:auto}h1{font-size:1.3rem}table{border-collapse:collapse;width:100%}"
    "td,th{border-bottom:1px solid #ddd;padding:.35rem .5rem;text-align:left;vertical-align:top}"
    ".box{border:1px solid #bbb;border-radius:8px;padding:1rem;margin:1rem 0}"
    "pre{white-space:pre-wrap;background:#f5f5f5;padding:.6rem;border-radius:6px;max-height:16rem;overflow:auto}"
    "button{font:inherit;padding:.4rem .9rem;border-radius:6px;border:1px solid #444;cursor:pointer}"
    "button.ok{background:#1b5e20;color:#fff;border-color:#1b5e20}"
    "input,select{font:inherit;padding:.3rem}.muted{color:#666}code{word-break:break-all}"
)
# Same-origin referrers keep the browser sending a real Origin on form posts (the CSRF check uses it).
HEADERS = {
    **SECURITY_HEADERS,
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'",
}


def redirect(url: str, status: int = 303) -> RedirectResponse:
    return RedirectResponse(url, status_code=status, headers=HEADERS)


def render(title: str, body: str, *, nav: str = "", status: int = 200) -> HTMLResponse:
    html = (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width'>"
        f"<title>{esc(title)} - Hive admin</title><style>{STYLE}</style><body>{nav}"
        f"<h1>{esc(title)}</h1>{body}</body></html>"
    )
    return HTMLResponse(html, status_code=status, headers=HEADERS)


def hidden(name: str, value: str) -> str:
    return f"<input type=hidden name={name} value='{esc(value, quote=True)}'>"


def nav_bar(links: list[tuple[str, str]], who: str, csrf: str) -> str:
    items = "".join(f"<a href='{esc(href, quote=True)}'>{esc(label)}</a>" for href, label in links)
    return (
        f"<nav><b>Hive admin</b>{items}<span class=muted>{esc(who)}</span>"
        f"<form method=post action=/admin/logout>{hidden('csrf', csrf)}<button>Sign out</button></form></nav>"
    )


def table(headers: list[str], rows: list[list[str]]) -> str:
    """Cells are already-escaped HTML fragments."""
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><tr>{head}</tr>{body}</table>"
