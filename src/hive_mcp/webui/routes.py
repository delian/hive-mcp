"""HTTP routes of the admin pages under /admin. Every POST is checked twice: the Origin header
must be ours and the form must carry the session's CSRF token."""

from __future__ import annotations

import hmac
from html import escape
from typing import TYPE_CHECKING, Literal, cast

from starlette.responses import Response
from starlette.routing import Route

from hive_mcp.domain.errors import HiveError, NotFound, PermissionDenied
from hive_mcp.domain.principal import Role
from hive_mcp.domain.promotion import PromotionStatus
from hive_mcp.webui import views
from hive_mcp.webui.layout import nav_bar, redirect, render

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request

    from hive_mcp.bootstrap import Services
    from hive_mcp.domain.principal import Principal
    from hive_mcp.webui.account_routes import AccountPages
    from hive_mcp.webui.session import AdminSession
    from hive_mcp.webui.signin import SignIn

AUDIT_PAGE = 100
HISTORY_PAGE = 50
HISTORY_MAX = 1000  # most recent requests considered
STATUS_FILTERS = ("all", "pending", "approved", "rejected", "withdrawn", "stale")
MESSAGES = {
    "approve": "Request approved; the change is applied shortly.",
    "reject": "Request rejected.",
}
ERROR_STATUS = ((PermissionDenied, 403), (NotFound, 404))


class AdminUi:
    def __init__(
        self,
        get_services: Callable[[], Services],
        signin: SignIn,
        quota: tuple[int, int] = (0, 0),
    ) -> None:
        self.get_services, self.signin, self.quota = get_services, signin, quota
        self.accounts: AccountPages | None = None  # set by build_admin_ui

    async def gate(
        self,
        request: Request,
        need: Role | None = None,
        *,
        approver: bool = False,
        post: bool = False,
    ) -> tuple[AdminSession, Principal] | Response:
        current = await self.signin.current(request)
        if current is None:
            return redirect("/admin/login")
        session, principal = current
        if post:
            form = await request.form()
            if not self.signin.origin_ok(request) or not hmac.compare_digest(
                str(form.get("csrf", "")).encode(), session.csrf.encode()
            ):
                return render(
                    "Not allowed", "<p>The form was not valid. Reload and retry.</p>", status=403
                )
        if (need and principal.role < need) or (approver and not principal.is_approver):
            return self.page("Not allowed", "<p>Your role cannot use this page.</p>", current, 403)
        return current

    def page(
        self, title: str, body: str, current: tuple[AdminSession, Principal], status: int = 200
    ) -> Response:
        session, principal = current
        links = [("/admin", "Home")]
        if principal.is_approver:
            links += [("/admin/promotions", "Approvals"), ("/admin/promotions/history", "History")]
        if principal.role >= Role.ADMIN:
            links += [
                ("/admin/users", "People"), ("/admin/tokens", "Tokens"),
                ("/admin/audit", "Audit"), ("/admin/usage", "Usage"),
            ]  # fmt: skip
        return render(title, body, nav=nav_bar(links, principal.id, session.csrf), status=status)

    # ------------------------------------------------------------ pages
    async def home(self, request: Request) -> Response:
        gate = await self.gate(request)
        if isinstance(gate, Response):
            return gate
        principal = gate[1]
        pending = 0
        if principal.is_approver:
            pending = len(await self.get_services().promotions.list(principal))
        body = views.dashboard(
            principal.id, principal.role.name.lower(), principal.is_approver,
            principal.role >= Role.ADMIN, pending,
        )  # fmt: skip
        return self.page("Overview", body, gate)

    async def promotions(self, request: Request) -> Response:
        gate = await self.gate(request, approver=True)
        if isinstance(gate, Response):
            return gate
        found = await self.get_services().promotions.list(gate[1], PromotionStatus.PENDING)
        message = MESSAGES.get(request.query_params.get("done", ""), "")
        return self.page("Promotion requests", views.promotions(found, gate[0].csrf, message), gate)

    async def history(self, request: Request) -> Response:
        gate = await self.gate(request, approver=True)
        if isinstance(gate, Response):
            return gate
        status = request.query_params.get("status", "all")
        status = status if status in STATUS_FILTERS else "all"
        try:
            page = max(int(request.query_params.get("page", "1")), 1)
        except ValueError:
            page = 1
        services = self.get_services()
        wanted = None if status == "all" else PromotionStatus(status)
        newest = await services.promotions.list(
            gate[1], wanted, limit=HISTORY_MAX, newest_first=True
        )
        window = newest[(page - 1) * HISTORY_PAGE : page * HISTORY_PAGE]
        rows = []
        for view in window:
            item = await services.backend.items.get_system(gate[1].org_id, view.promotion.item_id)
            rows.append((view.promotion, item.title if item else ""))
        more = len(newest) > page * HISTORY_PAGE
        return self.page(
            "Promotion history", views.promotion_history(rows, status, page, more), gate
        )

    async def decide(self, request: Request) -> Response:
        gate = await self.gate(request, approver=True, post=True)
        if isinstance(gate, Response):
            return gate
        form = await request.form()
        decision = str(form.get("decision", ""))
        if decision not in MESSAGES:  # the page only offers approve and reject
            return self.page("Could not decide", "<p>Unknown decision.</p>", gate, 400)
        try:
            await self.get_services().promotions.decide(
                gate[1],
                str(form.get("promotion", "")),
                cast("Literal['approve', 'reject']", decision),
                str(form.get("note", "")) or None,
            )
        except HiveError as exc:
            status = next((s for kind, s in ERROR_STATUS if isinstance(exc, kind)), 400)
            return self.page("Could not decide", f"<p>{escape(str(exc))}</p>", gate, status)
        return redirect(f"/admin/promotions?done={decision}")

    async def audit(self, request: Request) -> Response:
        gate = await self.gate(request, Role.ADMIN)
        if isinstance(gate, Response):
            return gate
        q = request.query_params
        filters = {k: q.get(k, "").strip() for k in ("actor", "action", "target")}
        try:
            before = max(int(q.get("before", "0")), 0) or None
        except ValueError:
            before = None
        rows = await self.get_services().admin.audit(
            gate[1], actor=filters["actor"], action=filters["action"], target=filters["target"],
            before_seq=before, newest_first=True, limit=AUDIT_PAGE,
        )  # fmt: skip
        more = (
            rows[-1].seq if len(rows) == AUDIT_PAGE else None
        )  # newest first: the last is the oldest
        return self.page("Audit log", views.audit(rows, filters, more), gate)

    async def usage(self, request: Request) -> Response:
        gate = await self.gate(request, Role.ADMIN)
        if isinstance(gate, Response):
            return gate
        rows = await self.get_services().admin.usage(gate[1])
        return self.page("Usage", views.usage(rows, *self.quota), gate)

    def routes(self) -> list[Route]:
        return [
            Route("/admin", self.home),
            *self.signin.routes(),
            Route("/admin/promotions", self.promotions),
            Route("/admin/promotions/history", self.history),
            Route("/admin/promotions/decide", self.decide, methods=["POST"]),
            Route("/admin/audit", self.audit),
            Route("/admin/usage", self.usage),
            *(self.accounts.routes() if self.accounts else []),
        ]
