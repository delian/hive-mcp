"""Routes of the people and tokens pages (admins only; every POST has the CSRF and Origin checks)."""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

from starlette.responses import Response
from starlette.routing import Route

from hive_mcp.domain.errors import HiveError, NotFound, PermissionDenied
from hive_mcp.domain.principal import Role
from hive_mcp.webui import views_accounts as views
from hive_mcp.webui.layout import redirect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.datastructures import FormData
    from starlette.requests import Request

    from hive_mcp.domain.principal import Principal
    from hive_mcp.webui.accounts import AccountAdmin
    from hive_mcp.webui.routes import AdminUi

DONE = {
    "saved": "Saved.", "disabled": "Disabled; their sessions ended.", "enabled": "Enabled.",
    "removed": "Removed.", "signedout": "Signed out everywhere.", "revoked": "Token revoked.",
}  # fmt: skip


def field_reader(form: FormData) -> Callable[[str], str]:
    def field(name: str) -> str:
        return str(form.get(name, ""))

    return field


class AccountPages:
    def __init__(self, ui: AdminUi, accounts: AccountAdmin) -> None:
        self.ui, self.accounts = ui, accounts

    async def _run(self, request: Request, action: str, done: str, back: str) -> Response:
        gate = await self.ui.gate(request, Role.ADMIN, post=True)
        if isinstance(gate, Response):
            return gate
        form = await request.form()
        actor = gate[1]
        field = field_reader(form)
        try:
            await self._do(action, actor, field, form)
        except HiveError as exc:
            status = (
                403
                if isinstance(exc, PermissionDenied)
                else 404
                if isinstance(exc, NotFound)
                else 400
            )
            return self.ui.page(
                "Not done",
                f"<p>{escape(str(exc))}</p><p><a href='{back}'>Back</a></p>",
                gate,
                status,
            )
        return redirect(f"{back}?done={done}")

    async def _do(
        self, action: str, actor: Principal, field: Callable[[str], str], form: FormData
    ) -> None:
        match action:
            case "save":
                await self.accounts.save_user(
                    actor, field("email"), field("role"), field("teams"), field("projects"),
                    field("approves"), disabled=bool(form.get("disabled")),
                    reset_identity=bool(form.get("reset_identity")),
                )  # fmt: skip
            case "disable" | "enable":
                await self.accounts.set_disabled(actor, field("email"), action == "disable")
            case "remove":
                await self.accounts.remove_user(actor, field("email"))
            case "signout":
                await self.accounts.sign_out_everywhere(actor, field("email"))
            case "revoke":
                await self.accounts.revoke_token(actor, field("label"))

    async def users(self, request: Request) -> Response:
        gate = await self.ui.gate(request, Role.ADMIN)
        if isinstance(gate, Response):
            return gate
        message = DONE.get(request.query_params.get("done", ""), "")
        body = views.users(self.accounts.users(gate[1]), gate[0].csrf, gate[1].id, message)
        return self.ui.page("People", body, gate)

    async def tokens(self, request: Request) -> Response:
        gate = await self.ui.gate(request, Role.ADMIN)
        if isinstance(gate, Response):
            return gate
        message = DONE.get(request.query_params.get("done", ""), "")
        body = views.tokens(self.accounts.tokens(gate[1]), gate[0].csrf, message)
        return self.ui.page("Tokens", body, gate)

    async def create_token(self, request: Request) -> Response:
        gate = await self.ui.gate(request, Role.ADMIN, post=True)
        if isinstance(gate, Response):
            return gate
        form = await request.form()
        field = field_reader(form)
        try:
            token = await self.accounts.create_token(
                gate[1], field("label"), field("subject"), field("role"), field("teams"),
                field("projects"), field("approves"), field("days"), field("rate"),
            )  # fmt: skip
        except HiveError as exc:
            body = f"<p>{escape(str(exc))}</p><p><a href=/admin/tokens>Back</a></p>"
            return self.ui.page("Not created", body, gate, 400)
        return self.ui.page("New token", views.new_token(field("label").strip(), token), gate)

    def routes(self) -> list[Route]:
        def post(action: str, done: str, back: str) -> Callable[[Request], Awaitable[Response]]:
            async def handler(request: Request) -> Response:
                return await self._run(request, action, done, back)

            return handler

        users_path = "/admin/users"
        return [
            Route(users_path, self.users),
            Route(f"{users_path}/save", post("save", "saved", users_path), methods=["POST"]),
            Route(
                f"{users_path}/disable", post("disable", "disabled", users_path), methods=["POST"]
            ),
            Route(f"{users_path}/enable", post("enable", "enabled", users_path), methods=["POST"]),
            Route(f"{users_path}/remove", post("remove", "removed", users_path), methods=["POST"]),
            Route(
                f"{users_path}/signout", post("signout", "signedout", users_path), methods=["POST"]
            ),
            Route("/admin/tokens", self.tokens),
            Route("/admin/tokens/create", self.create_token, methods=["POST"]),
            Route(
                "/admin/tokens/revoke", post("revoke", "revoked", "/admin/tokens"), methods=["POST"]
            ),
        ]
