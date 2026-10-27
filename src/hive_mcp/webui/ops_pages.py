"""Routes for the integrations, connectors, stale-notes and duplicates pages (admins; POSTs have
the CSRF and Origin checks via `AdminUi.gate`)."""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

from starlette.responses import Response
from starlette.routing import Route

from hive_mcp.connectors.factory import make_connector
from hive_mcp.connectors.spec import parse_specs
from hive_mcp.connectors.sync import STATUS_NS
from hive_mcp.domain.errors import HiveError, NotFound, PermissionDenied, ValidationError
from hive_mcp.domain.principal import Role
from hive_mcp.integrations.config import NAMES, SECRET_FIELDS
from hive_mcp.webui import views_ops as views
from hive_mcp.webui.layout import redirect

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request

    from hive_mcp.bootstrap import Services
    from hive_mcp.config import Settings
    from hive_mcp.webui.routes import AdminUi

DONE = {"saved": "Saved.", "ran": "Sync finished.", "confirmed": "Confirmed."}


def parse_mapping(text: str) -> dict[str, str]:
    """`key=value` per line; blank lines are skipped, anything else malformed is an error."""
    found: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip() or not value.strip():
            raise ValidationError(f"expected key=value, got {line!r}")
        found[key.strip()] = value.strip()
    return found


class OpsPages:
    def __init__(
        self, ui: AdminUi, get_services: Callable[[], Services], settings: Settings
    ) -> None:
        self.ui, self._services, self._settings = ui, get_services, settings

    def _failed(self, exc: HiveError, gate: tuple, back: str) -> Response:  # type: ignore[type-arg]
        status = (
            403 if isinstance(exc, PermissionDenied) else 404 if isinstance(exc, NotFound) else 400
        )
        return self.ui.page(
            "Not done", f"<p>{escape(str(exc))}</p><p><a href='{back}'>Back</a></p>", gate, status
        )

    # ------------------------------------------------------------------ integrations
    async def integrations(self, request: Request) -> Response:
        gate = await self.ui.gate(request, Role.ADMIN)
        if isinstance(gate, Response):
            return gate
        store = self._services().integrations
        assert store is not None  # noqa: S101
        configs = [await store.get(name) for name in NAMES]
        message = DONE.get(request.query_params.get("done", ""), "")
        body = views.integrations(
            configs, gate[0].csrf, message, self._settings.public_url.rstrip("/")
        )
        return self.ui.page("Integrations", body, gate)

    async def save_integration(self, request: Request) -> Response:
        gate = await self.ui.gate(request, Role.ADMIN, post=True)
        if isinstance(gate, Response):
            return gate
        form = await request.form()
        name = str(form.get("name", ""))
        store = self._services().integrations
        assert store is not None  # noqa: S101
        try:
            if name not in NAMES:
                raise NotFound(name)
            changes = {
                "enabled": bool(form.get("enabled")),
                "allow_mentions": bool(form.get("allow_mentions")),
                "workspace": str(form.get("workspace", "")).strip(),
                "app_id": str(form.get("app_id", "")).strip(),
                "channel_projects": parse_mapping(str(form.get("channel_projects", ""))),
                "users": parse_mapping(str(form.get("users", ""))),
            }
            if name != "teams":
                changes.pop("app_id")
            secrets = {}
            for field in SECRET_FIELDS[name]:
                if form.get(f"clear_{field}"):
                    secrets[field] = ""
                elif value := str(form.get(f"secret_{field}", "")):
                    secrets[field] = value
            await store.update(gate[1], name, changes, secrets)
        except HiveError as exc:
            return self._failed(exc, gate, "/admin/integrations")
        return redirect("/admin/integrations?done=saved")

    # ------------------------------------------------------------------ connectors
    async def connectors(self, request: Request) -> Response:
        gate = await self.ui.gate(request, Role.ADMIN)
        if isinstance(gate, Response):
            return gate
        services = self._services()
        rows = [
            (spec, await services.backend.kv.get(STATUS_NS, spec.name))
            for spec in parse_specs(self._settings.connectors)
        ]
        message = DONE.get(request.query_params.get("done", ""), "")
        return self.ui.page("Connectors", views.connectors(rows, gate[0].csrf, message), gate)

    async def run_connector(self, request: Request) -> Response:
        gate = await self.ui.gate(request, Role.ADMIN, post=True)
        if isinstance(gate, Response):
            return gate
        form = await request.form()
        name = str(form.get("name", ""))
        services = self._services()
        try:
            spec = next((s for s in parse_specs(self._settings.connectors) if s.name == name), None)
            if spec is None or services.connectors is None:
                raise NotFound(name)
            if spec.org != gate[1].org_id:
                raise PermissionDenied("that connector belongs to another organisation")
            await services.connectors.run(spec, make_connector(spec, self._settings))
        except HiveError as exc:
            return self._failed(exc, gate, "/admin/connectors")
        return redirect("/admin/connectors?done=ran")

    # ------------------------------------------------------------------ stale and duplicates
    async def stale(self, request: Request) -> Response:
        gate = await self.ui.gate(request, Role.ADMIN)
        if isinstance(gate, Response):
            return gate
        try:
            days = int(request.query_params.get("days", "180"))
        except ValueError:
            days = 180
        review = self._services().stale
        assert review is not None  # noqa: S101
        try:
            page = await review.stale(gate[1], days=days, limit=50, mine_only=False)
        except HiveError as exc:
            return self._failed(exc, gate, "/admin/stale")
        message = DONE.get(request.query_params.get("done", ""), "")
        return self.ui.page("Stale notes", views.stale(page, days, gate[0].csrf, message), gate)

    async def confirm_stale(self, request: Request) -> Response:
        gate = await self.ui.gate(request, Role.ADMIN, post=True)
        if isinstance(gate, Response):
            return gate
        form = await request.form()
        review = self._services().stale
        assert review is not None  # noqa: S101
        try:
            await review.confirm(gate[1], str(form.get("id", "")))
        except HiveError as exc:
            return self._failed(exc, gate, "/admin/stale")
        days = "".join(c for c in str(form.get("days", "180")) if c.isdigit()) or "180"
        return redirect(f"/admin/stale?days={days}&done=confirmed")

    async def duplicates(self, request: Request) -> Response:
        gate = await self.ui.gate(request, Role.ADMIN)
        if isinstance(gate, Response):
            return gate
        finder = self._services().duplicates
        assert finder is not None  # noqa: S101
        try:
            sweep = await finder.sweep(gate[1], scan=100)
            titles = {}
            for group in sweep.groups:
                for item_id in group.ids:
                    titles[item_id] = (
                        await self._services().knowledge.get(gate[1], item_id)
                    ).item.title
        except HiveError as exc:
            return self._failed(exc, gate, "/admin/duplicates")
        return self.ui.page("Duplicates", views.duplicates(sweep, titles), gate)

    def routes(self) -> list[Route]:
        return [
            Route("/admin/integrations", self.integrations),
            Route("/admin/integrations/save", self.save_integration, methods=["POST"]),
            Route("/admin/connectors", self.connectors),
            Route("/admin/connectors/run", self.run_connector, methods=["POST"]),
            Route("/admin/stale", self.stale),
            Route("/admin/stale/confirm", self.confirm_stale, methods=["POST"]),
            Route("/admin/duplicates", self.duplicates),
        ]
