"""Sign-in and sign-out for the admin UI: a pasted static token, or Google Workspace."""

from __future__ import annotations

import hmac
import logging
import secrets
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from starlette.routing import Route

from hive_mcp.domain.clock import SystemClock
from hive_mcp.ports.audit import AuditEvent
from hive_mcp.security.oauth.login import Denied, check_identity
from hive_mcp.security.oauth.upstream import UpstreamError
from hive_mcp.webui import views
from hive_mcp.webui.layout import redirect, render
from hive_mcp.webui.session import COOKIE_NAME, SESSION_SECONDS

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request
    from starlette.responses import Response

    from hive_mcp.bootstrap import Services
    from hive_mcp.domain.principal import Principal
    from hive_mcp.security.directory import UserDirectory
    from hive_mcp.security.oauth.cookies import CookieSigner
    from hive_mcp.security.oauth.upstream import GoogleOidc
    from hive_mcp.webui.auth import AdminAuth
    from hive_mcp.webui.session import AdminSession, SessionCodec

log = logging.getLogger("hive.security")
NS_TXN = "admin:txn"
TXN_COOKIE = "hive_admin_txn"
TXN_SECONDS = 600


class SignIn:
    def __init__(  # noqa: PLR0913 - collaborators
        self,
        get_services: Callable[[], Services],
        auth: AdminAuth,
        codec: SessionCodec,
        signer: CookieSigner,
        *,
        public_url: str,
        upstream: GoogleOidc | None = None,
        allowed_domains: list[str] | None = None,
        directory: UserDirectory | None = None,
    ) -> None:
        self.get_services, self.auth, self.codec, self.signer = get_services, auth, codec, signer
        self.upstream, self.allowed_domains, self.directory = (
            upstream,
            allowed_domains or [],
            directory,
        )
        parsed = urlparse(public_url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.secure = parsed.scheme == "https"

    def origin_ok(self, request: Request) -> bool:
        return request.headers.get("origin") == self.origin

    def set_cookie(self, response: Response, name: str, value: str, max_age: int) -> None:
        response.set_cookie(
            name, value, max_age=max_age, path="/admin", httponly=True, secure=self.secure,
            samesite="lax",  # strict would drop it on the redirect that follows Google's callback
        )  # fmt: skip

    async def start_session(self, kind: str, subject: str, principal: Principal) -> Response:
        services = self.get_services()
        event = AuditEvent(principal.org_id, principal.id, "admin.login", SystemClock().now())
        await services.backend.audit.append(event)
        log.info("admin.login kind=%s subject=%s", kind, subject)
        response = redirect("/admin")
        self.set_cookie(response, COOKIE_NAME, self.codec.issue(kind, subject), SESSION_SECONDS)
        return response

    def current(self, request: Request) -> tuple[AdminSession, Principal] | None:
        session = self.codec.read(request.cookies.get(COOKIE_NAME))
        principal = self.auth.principal(session) if session else None
        return (session, principal) if session and principal else None

    def login_page(self, notice: str = "", status: int = 200) -> Response:
        return render(
            "Sign in",
            views.login_form(google=self.upstream is not None, notice=notice),
            status=status,
        )

    async def login(self, request: Request) -> Response:
        if request.method == "GET":
            return self.login_page()
        form = await request.form()
        label = (
            self.auth.token_label(str(form.get("token", ""))) if self.origin_ok(request) else None
        )
        principal = self.auth.principal_for_label(label) if label else None
        if label is None or principal is None:
            log.warning("admin.login_denied kind=token")
            return self.login_page("That token is not valid.", 401)
        return await self.start_session("token", label, principal)

    async def logout(self, request: Request) -> Response:
        current = self.current(request)
        if (
            current
            and self.origin_ok(request)
            and hmac.compare_digest(str((await request.form()).get("csrf", "")), current[0].csrf)
        ):
            response = redirect("/admin/login")
            response.delete_cookie(COOKIE_NAME, path="/admin")
            return response
        return redirect("/admin")

    async def google_start(self, _: Request) -> Response:
        if self.upstream is None:
            return self.login_page("Google sign-in is not configured.", 404)
        txn = secrets.token_urlsafe(24)
        data = {"nonce": secrets.token_urlsafe(24), "verifier": secrets.token_urlsafe(48)}
        await self.get_services().backend.kv.put(NS_TXN, txn, data, ttl_seconds=TXN_SECONDS)
        hosted = self.allowed_domains[0] if len(self.allowed_domains) == 1 else None
        try:
            url = await self.upstream.authorization_url(
                state=txn, nonce=data["nonce"], code_verifier=data["verifier"], hosted_domain=hosted
            )
        except UpstreamError:
            return self.login_page("The sign-in service is unavailable.", 502)
        response = redirect(url, 302)
        self.set_cookie(response, TXN_COOKIE, self.signer.sign(txn), TXN_SECONDS)
        return response

    async def google_callback(self, request: Request) -> Response:
        state = request.query_params.get("state", "")
        if self.upstream is None or not self.signer.verify(request.cookies.get(TXN_COOKIE), state):
            return self.login_page("This sign-in was not started in this browser.", 403)
        txn = await self.get_services().backend.kv.take(NS_TXN, state)  # single use
        code = request.query_params.get("code")
        if txn is None or not code:
            return self.login_page("Sign-in was cancelled or expired.", 400)
        try:
            identity = await self.upstream.exchange(
                code=code, code_verifier=txn["verifier"], nonce=txn["nonce"]
            )
        except UpstreamError:
            return self.login_page("Sign-in could not be verified.", 502)
        assert self.directory is not None  # noqa: S101 - set whenever upstream is
        outcome = check_identity(identity, self.allowed_domains, self.directory)
        if isinstance(outcome, Denied):
            log.warning("admin.login_denied reason=%s", outcome.reason)
            return self.login_page("This account may not sign in.", 403)
        response = await self.start_session("user", outcome.email, outcome.principal())
        response.delete_cookie(TXN_COOKIE, path="/admin")
        return response

    def routes(self) -> list[Route]:
        return [
            Route("/admin/login", self.login, methods=["GET", "POST"]),
            Route("/admin/login/google", self.google_start),
            Route("/admin/google/callback", self.google_callback),
            Route("/admin/logout", self.logout, methods=["POST"]),
        ]
