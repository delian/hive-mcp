"""Pages for integrations, connectors, stale notes and duplicates. Dynamic text goes through esc."""

from __future__ import annotations

from html import escape as esc
from typing import TYPE_CHECKING, Any

from hive_mcp.webui.layout import hidden, table

if TYPE_CHECKING:
    from hive_mcp.connectors.spec import ConnectorSpec
    from hive_mcp.integrations.config import IntegrationConfig
    from hive_mcp.services.duplicates import DuplicateSweep
    from hive_mcp.services.staleness import StalePage


def _lines(mapping: dict[str, str]) -> str:
    return esc("\n".join(f"{k}={v}" for k, v in sorted(mapping.items())))


def _checked(on: bool) -> str:
    return " checked" if on else ""


def _secret_row(config: IntegrationConfig, field: str, label: str) -> str:
    state = "stored" if config.secrets.get(field) else "not set"
    return (
        f"<p><label>{esc(label)} ({state}) <input type=password name=secret_{field} "
        f"autocomplete=off placeholder='leave empty to keep'></label> "
        f"<label><input type=checkbox name=clear_{field} value=1> remove</label></p>"
    )


def integration_form(config: IntegrationConfig, csrf: str) -> str:
    teams = config.name == "teams"
    workspace = "Teams tenant id (Entra directory id)" if teams else "Slack workspace id (T...)"
    secrets = (
        _secret_row(config, "app_password", "Bot app password (client secret)")
        if teams
        else _secret_row(config, "signing_secret", "Signing secret")
        + _secret_row(config, "bot_token", "Bot token (xoxb-...)")
    )
    app = (
        f"<p><label>Bot application id <input name=app_id value='{esc(config.app_id, quote=True)}'></label></p>"
        if teams
        else ""
    )
    return (
        f"<div class=box><b>{'Microsoft Teams' if teams else 'Slack'}</b>"
        f"<form method=post action=/admin/integrations/save>{hidden('csrf', csrf)}"
        f"{hidden('name', config.name)}"
        f"<p><label><input type=checkbox name=enabled value=1{_checked(config.enabled)}> enabled</label> "
        f"<label><input type=checkbox name=allow_mentions value=1{_checked(config.allow_mentions)}> "
        "answer when mentioned in a channel (in a thread)</label></p>"
        f"<p><label>{workspace} <input name=workspace value='{esc(config.workspace, quote=True)}'></label></p>"
        f"{app}{secrets}"
        "<p><label>Channels that share into a project, one <code>channel-id=project</code> per line<br>"
        f"<textarea name=channel_projects rows=4 cols=60>{_lines(config.channel_projects)}</textarea></label></p>"
        "<p><label>People without a bot token, one <code>chat-user-id=email</code> per line<br>"
        f"<textarea name=users rows=3 cols=60>{_lines(config.users)}</textarea></label></p>"
        "<button class=ok>Save</button></form></div>"
    )


def integrations(configs: list[IntegrationConfig], csrf: str, message: str, base_url: str) -> str:
    intro = (
        f"<p class=muted>Request URLs to give the chat service: Slack <code>{esc(base_url)}/slack/commands</code> "
        f"and <code>{esc(base_url)}/slack/interactions</code>; Teams <code>{esc(base_url)}/teams/messages</code>. "
        "Changes take effect within seconds. Secrets are stored encrypted and never shown again.</p>"
    )
    notice = f"<p class=box>{esc(message)}</p>" if message else ""
    return notice + intro + "".join(integration_form(c, csrf) for c in configs)


def connectors(
    rows: list[tuple[ConnectorSpec, dict[str, Any] | None]], csrf: str, message: str
) -> str:
    notice = f"<p class=box>{esc(message)}</p>" if message else ""
    if not rows:
        return notice + "<p>No connectors configured (HIVE_CONNECTORS, see docs/CONNECTORS.md).</p>"
    body = []
    for spec, last in rows:
        status = f"{last['summary']} ({last['at']})" if last else "never ran"
        failed = "; ".join(f"{d}: {w}" for d, w in last.get("failed", {}).items()) if last else ""
        run = (
            f"<form method=post action=/admin/connectors/run style='display:inline'>"
            f"{hidden('csrf', csrf)}{hidden('name', spec.name)}<button>Run now</button></form>"
        )
        body.append(
            [esc(spec.name), esc(spec.type), esc(spec.project), f"{spec.every_minutes} min",
             esc(status) + (f"<br><span class=muted>{esc(failed)}</span>" if failed else ""), run]
        )  # fmt: skip
    return notice + table(["name", "type", "project", "every", "last run", ""], body)


def stale(page: StalePage, days: int, csrf: str, message: str) -> str:
    notice = f"<p class=box>{esc(message)}</p>" if message else ""
    rows = []
    for s in page.items:
        confirm = (
            f"<form method=post action=/admin/stale/confirm style='display:inline'>"
            f"{hidden('csrf', csrf)}{hidden('id', s.item.id)}{hidden('days', str(days))}"
            "<button>Still true</button></form>"
        )
        rows.append(
            [esc(s.item.title), esc(s.item.author_id), esc(s.last_touched.isoformat(timespec="days")),
             f"<code>{esc(s.item.id)}</code>", confirm]
        )  # fmt: skip
    more = (
        "<p class=muted>More exist: raise the age or confirm some first.</p>"
        if page.next_cursor
        else ""
    )
    head = (
        f"<form method=get action=/admin/stale>Not edited or confirmed for <input name=days value={days} size=4> "
        "days <button>Show</button></form>"
    )
    return notice + head + table(["note", "author", "last touched", "id", ""], rows) + more


def duplicates(sweep: DuplicateSweep, titles: dict[str, str]) -> str:
    if not sweep.groups:
        return f"<p>No near-duplicates among the {sweep.scanned} newest notes you can edit.</p>"
    rows = [
        [
            "<br>".join(f"{esc(titles.get(i, '?'))} <code>{esc(i)}</code>" for i in g.ids),
            f"{g.best_score:.2f}", "identical" if g.exact else "similar",
        ]
        for g in sweep.groups
    ]  # fmt: skip
    note = (
        "<p class=muted>Merge from your agent: read the group, write one note, call kb_merge.</p>"
    )
    return f"<p>{sweep.scanned} notes scanned.</p>" + table(["notes", "score", "kind"], rows) + note
