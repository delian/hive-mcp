"""HTTP routes of the browser login: /oauth/start, /oauth/google/callback, /oauth/consent."""

from __future__ import annotations

import hmac
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from hive_mcp.security.oauth.cookies import COOKIE_NAME
from hive_mcp.security.oauth.login import Denied, check_identity, pin_identity
from hive_mcp.security.oauth.models import TXN_TTL_SECONDS
from hive_mcp.security.oauth.pages import SECURITY_HEADERS, consent_page, error_page
from hive_mcp.security.oauth.revoke import revoke_route
from hive_mcp.security.oauth.upstream import UpstreamError

if TYPE_CHECKING:
    from starlette.requests import Request

    from hive_mcp.ports.kv import JsonObject
    from hive_mcp.security.oauth.cookies import CookieSigner
    from hive_mcp.security.oauth.provider import HiveOAuthProvider
    from hive_mcp.security.oauth.upstream import GoogleOidc

log = logging.getLogger("hive.security")


def redirect(url: str, status: int = 302) -> RedirectResponse:
    return RedirectResponse(url, status_code=status, headers=SECURITY_HEADERS)


class LoginRoutes:
    def __init__(
        self,
        provider: HiveOAuthProvider,
        upstream: GoogleOidc,
        signer: CookieSigner,
        allowed_domains: list[str],
    ) -> None:
        self.provider, self.upstream, self.signer = provider, upstream, signer
        self.allowed_domains = allowed_domains
        self.secure_cookie = provider.config.public_url.startswith("https://")
        self.hosted = allowed_domains[0] if len(allowed_domains) == 1 else None

    def bound(self, request: Request, txn_id: str) -> bool:
        """Was this login started in this browser? (the signed cookie is bound to the transaction)"""
        return self.signer.verify(request.cookies.get(COOKIE_NAME), txn_id)

    async def start(self, request: Request) -> Response:
        txn_id = request.query_params.get("txn", "")
        txn = await self.provider.load_txn(txn_id) if txn_id else None
        if txn is None:
            return error_page("This sign-in link has expired.")
        try:
            url = await self.upstream.authorization_url(
                state=txn_id,
                nonce=txn["nonce"],
                code_verifier=txn["upstream_verifier"],
                hosted_domain=self.hosted,
            )
        except UpstreamError:
            log.exception("oauth.upstream_unavailable")
            return error_page("The sign-in service is unavailable. Try again shortly.", 502)
        response = redirect(url)
        response.set_cookie(
            COOKIE_NAME,
            self.signer.sign(txn_id),
            max_age=TXN_TTL_SECONDS,
            path="/oauth",
            httponly=True,
            secure=self.secure_cookie,
            samesite="lax",
        )
        return response

    async def callback(self, request: Request) -> Response:
        state = request.query_params.get("state", "")
        if not self.bound(request, state):  # login CSRF: this browser did not start this login
            log.warning("oauth.callback_cookie_mismatch")
            return error_page("This sign-in was not started in this browser.")
        txn = await self.provider.load_txn(state)
        if txn is None:
            return error_page("This sign-in has expired.")
        code = request.query_params.get("code")
        if request.query_params.get("error") or not code:
            return redirect(await self.provider.finish(state, approve=False) or "/")
        who = await self.verified_email(state, txn, code)
        if isinstance(who, Response):
            return who
        email = who
        await self.provider.attach_user(state, txn, email)
        log.info("oauth.login user=%s", email)
        if await self.provider.has_consent({**txn, "user": email}):
            log.info("oauth.consent_remembered user=%s client=%s", email, txn["client_id"])
            return await self.complete(state, approve=True)
        return redirect(f"/oauth/consent?txn={state}")

    async def verified_email(self, state: str, txn: JsonObject, code: str) -> str | Response:
        """The Google account's email if it may sign in, else the response to send back."""
        try:
            identity = await self.upstream.exchange(
                code=code, code_verifier=txn["upstream_verifier"], nonce=txn["nonce"]
            )
        except UpstreamError as exc:
            log.warning("oauth.upstream_rejected reason=%s", exc)
            await self.provider.finish(state, approve=False, error="server_error")
            return error_page("Sign-in could not be verified.", 502)
        outcome = check_identity(identity, self.allowed_domains, self.provider.directory)
        if isinstance(outcome, Denied):
            log.warning("oauth.login_denied reason=%s", outcome.reason)
            return redirect(
                await self.provider.finish(state, approve=False) or "/"
            )  # client sees only access_denied
        pin_identity(self.provider.directory, outcome, identity)
        return outcome.email

    async def complete(self, txn_id: str, *, approve: bool) -> Response:
        url = await self.provider.finish(txn_id, approve=approve)
        if url is None:
            return error_page("This sign-in has expired.")
        response = redirect(url, 303)
        response.delete_cookie(COOKIE_NAME, path="/oauth")
        return response

    async def consent(self, request: Request) -> Response:
        form = await request.form() if request.method == "POST" else None
        txn_id = str((form or request.query_params).get("txn", ""))
        if not self.bound(request, txn_id):
            return error_page("This approval page was not opened from your sign-in.", 403)
        txn = await self.provider.load_txn(txn_id)
        if txn is None or not txn.get("user"):
            return error_page("This sign-in has expired.")
        if form is None:
            return await self.consent_form(txn_id, txn)
        if not hmac.compare_digest(str(form.get("csrf", "")).encode(), str(txn["csrf"]).encode()):
            return error_page("The approval form was not valid.", 403)
        approve = form.get("decision") == "approve"
        if approve:
            await self.provider.remember_consent(txn)
        log.info(
            "oauth.consent user=%s client=%s approved=%s", txn["user"], txn["client_id"], approve
        )
        return await self.complete(txn_id, approve=approve)

    async def consent_form(self, txn_id: str, txn: dict[str, object]) -> Response:
        client = await self.provider.get_client(str(txn["client_id"]))
        target = urlparse(str(txn["redirect_uri"]))
        return consent_page(
            app_name=client.client_name
            if client and client.client_name
            else "(unnamed application)",
            redirect_host=f"{target.scheme}://{target.netloc}"
            if target.netloc
            else target.scheme + ":",
            email=str(txn["user"]),
            scopes=list(txn["scopes"]),  # type: ignore[call-overload]
            txn=txn_id,
            csrf=str(txn["csrf"]),
        )

    def routes(self) -> list[Route]:
        return [
            revoke_route(self.provider),
            Route("/oauth/start", self.start, methods=["GET"]),
            Route("/oauth/google/callback", self.callback, methods=["GET"]),
            Route("/oauth/consent", self.consent, methods=["GET", "POST"]),
        ]


def oauth_routes(
    provider: HiveOAuthProvider,
    upstream: GoogleOidc,
    signer: CookieSigner,
    allowed_domains: list[str],
) -> list[Route]:
    return LoginRoutes(provider, upstream, signer, allowed_domains).routes()
