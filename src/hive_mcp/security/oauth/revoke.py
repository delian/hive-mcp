"""RFC 7009 token revocation. Replaces the SDK route, whose request model demands a client_secret even
from public (PKCE, secret-less) clients, which is nearly every MCP client."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from hive_mcp.security.oauth.models import ACCESS_PREFIX

if TYPE_CHECKING:
    from mcp.server.auth.provider import AccessToken, RefreshToken
    from starlette.requests import Request

    from hive_mcp.security.oauth.provider import HiveOAuthProvider

log = logging.getLogger("hive.security")
MAX_FORM_BYTES = 64 * 1024


def revoke_route(provider: HiveOAuthProvider) -> Route:
    async def revoke(request: Request) -> Response:
        if int(request.headers.get("content-length") or 0) > MAX_FORM_BYTES:
            return JSONResponse({"error": "invalid_request"}, status_code=413)
        try:
            client = await ClientAuthenticator(provider).authenticate_request(request)
        except AuthenticationError as exc:
            return JSONResponse(
                {"error": "unauthorized_client", "error_description": exc.message}, status_code=401
            )
        token = str((await request.form()).get("token") or "")
        if not token:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "token is required"},
                status_code=400,
            )
        found: AccessToken | RefreshToken | None = None
        if token.startswith(ACCESS_PREFIX):
            found = await provider.load_access_token(token)
        found = found or await provider.load_refresh_token(client, token)
        if (
            found is not None and found.client_id == client.client_id
        ):  # clients revoke only their own tokens
            await provider.revoke_token(found)
            log.info("oauth.revoked client=%s", client.client_id)
        return Response(
            status_code=200, headers={"Cache-Control": "no-store"}
        )  # unknown tokens: still 200

    return Route("/revoke", revoke, methods=["POST"])
