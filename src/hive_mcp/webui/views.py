"""The admin pages' bodies. Each function returns HTML built only from escaped values."""

from __future__ import annotations

from html import escape as esc
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from hive_mcp.webui.layout import hidden, table

if TYPE_CHECKING:
    from hive_mcp.domain.promotion import Promotion
    from hive_mcp.ports.audit import AuditRecord
    from hive_mcp.ports.maintenance import AuthorUsage
    from hive_mcp.services.promotions import PromotionView

PREVIEW_CHARS = 1500


def login_form(*, google: bool, notice: str = "") -> str:
    parts = [f"<p>{esc(notice)}</p>"] if notice else []
    if google:
        parts.append(
            "<p><a href=/admin/login/google><button class=ok>Sign in with Google</button></a></p>"
        )
    parts.append(
        "<div class=box><p>Or paste an access token (<code>hive token create</code>):</p>"
        "<form method=post action=/admin/login><input type=password name=token size=48 "
        "autocomplete=off required> <button>Sign in</button></form></div>"
    )
    return "".join(parts)


def dashboard(who: str, role: str, can_approve: bool, can_audit: bool, pending: int) -> str:
    lines = [f"<p>Signed in as <b>{esc(who)}</b> ({esc(role)}).</p>"]
    if can_approve:
        lines.append(f"<p><a href=/admin/promotions>{pending} promotion request(s) waiting</a></p>")
    if can_audit:
        lines.append("<p><a href=/admin/audit>Audit log</a> · <a href=/admin/usage>Usage</a></p>")
    if not (can_approve or can_audit):
        lines.append("<p class=muted>Your role has no admin pages.</p>")
    return "".join(lines)


def _target(p: PromotionView) -> str:
    scope = p.promotion.scope
    return f"{scope.level.name.lower()}:{scope.owner} ({p.promotion.visibility})"


def _decision_form(promotion_id: str, csrf: str) -> str:
    return (
        f"<form method=post action=/admin/promotions/decide>{hidden('csrf', csrf)}"
        f"{hidden('promotion', promotion_id)}"
        "<input name=note placeholder='note (optional)' maxlength=500>"
        "<button class=ok name=decision value=approve>Approve</button>"
        "<button name=decision value=reject>Reject</button></form>"
    )


def promotions(views: list[PromotionView], csrf: str, message: str = "") -> str:
    out = [f"<p class=box>{esc(message)}</p>"] if message else []
    if not views:
        return "".join(out) + "<p class=muted>Nothing is waiting for approval.</p>"
    for view in views:
        promo, item = view.promotion, view.item
        title = item.title if item else promo.item_id
        preview = f"<pre>{esc(item.body[:PREVIEW_CHARS])}</pre>" if item else ""
        out.append(
            f"<div class=box><b>{esc(title)}</b> <span class=muted>revision {promo.item_revision}"
            f" · requested by {esc(promo.requester_id)}</span>"
            f"<p>Move to <b>{esc(_target(view))}</b>"
            f"{' - ' + esc(promo.reason) if promo.reason else ''}</p>{preview}"
            f"{_decision_form(promo.id, csrf)}</div>"
        )
    return "".join(out)


def audit(records: list[AuditRecord], filters: dict[str, str], next_seq: int | None) -> str:
    form = (
        "<form method=get action=/admin/audit>"
        + "".join(
            f"<input name={n} placeholder={n} value='{esc(filters.get(n, ''), quote=True)}'> "
            for n in ("actor", "action", "target")
        )
        + "<button>Filter</button></form>"
    )
    rows = [
        [
            str(r.seq),
            esc(r.event.at.isoformat(timespec="seconds")),
            esc(r.event.actor_id),
            esc(r.event.action),
            esc(r.event.target_id or ""),
            esc(", ".join(f"{k}={v}" for k, v in sorted(r.event.details.items()))),
        ]
        for r in records
    ]
    more = ""
    if next_seq is not None:
        query = urlencode({**{k: v for k, v in filters.items() if v}, "before": next_seq})
        more = f"<p><a href='/admin/audit?{esc(query, quote=True)}'>Older events</a></p>"
    cols = ["#", "when (UTC)", "who", "what", "target", "details"]
    return form + table(cols, rows) + more + "<p class=muted>Newest first.</p>"


def usage(rows: list[AuthorUsage], max_items: int, max_chars: int) -> str:
    limits = (
        f"<p class=muted>Quota per user: {max_items or 'unlimited'} items, "
        f"{max_chars or 'unlimited'} characters (admins are exempt).</p>"
    )
    body = [
        [esc(u.author_id), str(u.items), str(u.chars), esc(u.last_at.isoformat(timespec="seconds"))]
        for u in rows
    ]
    return limits + table(["author", "items", "characters", "last write"], body)


def promotion_history(
    rows: list[tuple[Promotion, str]], status: str, page: int, has_more: bool
) -> str:
    """`rows`: (request, item title or '' when the item no longer exists), newest request first."""
    options = "".join(
        f"<option{' selected' if s == status else ''}>{s}</option>"
        for s in ("all", "pending", "approved", "rejected", "withdrawn", "stale")
    )
    form = f"<form method=get action=/admin/promotions/history><select name=status>{options}</select> <button>Show</button></form>"
    body = []
    for promo, title in rows:
        scope = promo.scope
        body.append(
            [
                esc(promo.created_at.isoformat(timespec="minutes")),
                f"{esc(title) or '<span class=muted>(deleted)</span>'}<br><code>{esc(promo.item_id)}</code>",
                esc(promo.requester_id),
                esc(f"{scope.level.name.lower()}:{scope.owner} ({promo.visibility})"),
                esc(str(promo.status)),
                esc(promo.decided_by or "-"),
                esc(promo.decided_at.isoformat(timespec="minutes") if promo.decided_at else "-"),
                esc(promo.note or promo.reason or "-"),
            ]
        )
    cols = ["requested", "item", "by", "to", "status", "decided by", "decided", "note / reason"]
    nav = ""
    if page > 1 or has_more:
        links = []
        if page > 1:
            links.append(
                f"<a href='/admin/promotions/history?status={esc(status)}&amp;page={page - 1}'>Newer</a>"
            )
        if has_more:
            links.append(
                f"<a href='/admin/promotions/history?status={esc(status)}&amp;page={page + 1}'>Older</a>"
            )
        nav = "<p>" + " · ".join(links) + "</p>"
    return (
        form
        + table(cols, body)
        + nav
        + "<p class=muted>Newest first. Notes are the decider's; the reason is the requester's.</p>"
    )
