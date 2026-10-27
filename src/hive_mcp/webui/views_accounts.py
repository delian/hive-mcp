"""Bodies of the people and tokens pages (every value escaped)."""

from __future__ import annotations

import time
from html import escape as esc
from typing import TYPE_CHECKING

from hive_mcp.webui.layout import hidden, table

if TYPE_CHECKING:
    from hive_mcp.security.directory import DirectoryUser
    from hive_mcp.security.tokens import TokenRecord

ROLES = ("viewer", "member", "editor", "admin")


def _post(action: str, csrf: str, fields: dict[str, str], label: str, extra: str = "") -> str:
    inputs = "".join(hidden(k, v) for k, v in {"csrf": csrf, **fields}.items())
    return f"<form method=post action='{action}' style='display:inline'>{inputs}<button {extra}>{esc(label)}</button></form>"


def _role_select(selected: str = "member") -> str:
    options = "".join(f"<option{' selected' if r == selected else ''}>{r}</option>" for r in ROLES)
    return f"<select name=role>{options}</select>"


def _user_form(csrf: str, user: DirectoryUser | None = None) -> str:
    u = user
    return (
        "<form method=post action=/admin/users/save>"
        + hidden("csrf", csrf)
        + f"<input name=email placeholder='email' required value='{esc(u.email if u else '', quote=True)}'> "
        + _role_select(u.role.name.lower() if u else "member")
        + f" <input name=teams placeholder='teams' value='{esc(' '.join(sorted(u.teams)) if u else '', quote=True)}'>"
        + f" <input name=projects placeholder='projects' value='{esc(' '.join(sorted(u.projects)) if u else '', quote=True)}'>"
        + f" <input name=approves placeholder='approves e.g. team:eng' value='{esc(' '.join(sorted(u.approves)) if u else '', quote=True)}'>"
        + f" <label><input type=checkbox name=disabled value=1{' checked' if u and u.disabled else ''}> disabled</label>"
        + " <label><input type=checkbox name=reset_identity value=1> forget pinned Google account</label>"
        + " <button class=ok>Save</button></form>"
    )


def users(rows: list[DirectoryUser], csrf: str, me: str, message: str = "") -> str:
    out = [f"<p class=box>{esc(message)}</p>"] if message else []
    out.append(
        "<div class=box><b>Add or change a person</b> (same email = change)"
        + _user_form(csrf)
        + "</div>"
    )
    body = []
    for u in rows:
        who = {"email": u.email}
        actions = _post("/admin/users/signout", csrf, who, "Sign out everywhere")
        actions += _post(
            "/admin/users/enable" if u.disabled else "/admin/users/disable",
            csrf,
            who,
            "Enable" if u.disabled else "Disable",
        )
        if u.email != me:
            actions += _post(
                "/admin/users/remove",
                csrf,
                who,
                "Remove",
                'onclick="return false"' if False else "",
            )
        body.append(
            [
                esc(u.email) + (" <span class=muted>(you)</span>" if u.email == me else ""),
                esc(u.role.name.lower()) + (" - <b>disabled</b>" if u.disabled else ""),
                esc(" ".join(sorted(u.teams)) or "-"),
                esc(" ".join(sorted(u.projects)) or "-"),
                esc(" ".join(sorted(u.approves)) or "-"),
                "pinned" if u.google_sub else "-",
                actions,
            ]
        )
    cols = ["email", "role", "teams", "projects", "approves", "Google", "actions"]
    return (
        "".join(out)
        + table(cols, body)
        + "<p class=muted>Changes apply to live sessions and tokens at once.</p>"
    )


def _words(value: object) -> str:
    items = value if isinstance(value, list) else []
    return " ".join(str(i) for i in items) or "-"


def _expires(record: TokenRecord) -> str:
    if record.expires_at is None:
        return "never"
    return time.strftime("%Y-%m-%d", time.gmtime(record.expires_at))


def tokens(rows: list[TokenRecord], csrf: str, message: str = "") -> str:
    out = [f"<p class=box>{esc(message)}</p>"] if message else []
    out.append(
        "<div class=box><b>New token</b>"
        "<form method=post action=/admin/tokens/create>"
        + hidden("csrf", csrf)
        + "<input name=label placeholder='label (unique)' required> "
        "<input name=subject placeholder='acts as (user or service)' required> "
        + _role_select("member")
        + " <input name=teams placeholder='teams'> <input name=projects placeholder='projects'>"
        " <input name=approves placeholder='approves'> <input name=days placeholder='days (blank = never)' size=8>"
        " <input name=rate placeholder='requests/min (blank = default)' size=14>"
        " <button class=ok>Create</button></form>"
        "<p class=muted>The token is shown once, on the next page.</p></div>"
    )
    body = [
        [
            esc(t.label),
            esc(t.subject),
            esc(str(t.claims.get("hive_role", ""))),
            esc(_words(t.claims.get("hive_teams"))),
            esc(_words(t.claims.get("hive_projects"))),
            _expires(t),
            f"{t.rate_per_min}/min" if t.rate_per_min else "default",
            _post("/admin/tokens/revoke", csrf, {"label": t.label}, "Revoke"),
        ]
        for t in rows
    ]
    cols = ["label", "acts as", "role", "teams", "projects", "expires", "limit", "actions"]
    return "".join(out) + table(cols, body)


def new_token(label: str, token: str) -> str:
    return (
        f"<p>Token <b>{esc(label)}</b> created. Copy it now: it is not shown again and cannot be recovered.</p>"
        f"<pre>{esc(token)}</pre><p><a href=/admin/tokens>Back to tokens</a></p>"
    )
