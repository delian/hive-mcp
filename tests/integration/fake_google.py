"""A tiny OpenID Connect provider standing in for Google: real RSA signing, PKCE and client-secret checks."""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from tests.integration.util import free_port

if TYPE_CHECKING:
    from starlette.requests import Request


def b64(digest: bytes) -> str:
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


class FakeGoogle:
    client_id = "google-client"
    client_secret = "google-secret"

    def __init__(self) -> None:
        self.port = free_port()
        self.issuer = f"http://127.0.0.1:{self.port}"
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.signing_key = self.key  # tests swap this to forge signatures
        self.kid = "key-1"
        self.account: dict[str, Any] = {
            "email": "alice@acme.com",
            "email_verified": True,
            "hd": "acme.com",
            "sub": "g-alice",
        }
        self.claim_overrides: dict[str, Any] = {}
        self.header_overrides: dict[str, Any] = {}
        self.deny_login = False
        self.codes: dict[str, dict[str, str]] = {}
        self.authorize_requests: list[dict[str, str]] = []
        routes = [
            Route("/.well-known/openid-configuration", self.discovery),
            Route("/authorize", self.authorize),
            Route("/token", self.token, methods=["POST"]),
            Route("/jwks", self.jwks),
        ]
        config = uvicorn.Config(
            Starlette(routes=routes), host="127.0.0.1", port=self.port, log_level="error"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("fake google did not start")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    async def discovery(self, _: Request) -> Response:
        return JSONResponse(
            {
                "issuer": self.issuer,
                "authorization_endpoint": f"{self.issuer}/authorize",
                "token_endpoint": f"{self.issuer}/token",
                "jwks_uri": f"{self.issuer}/jwks",
            }
        )

    async def jwks(self, _: Request) -> Response:
        jwk = RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return JSONResponse({"keys": [{**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"}]})

    async def authorize(self, request: Request) -> Response:
        q = dict(request.query_params)
        self.authorize_requests.append(q)
        assert q["client_id"] == self.client_id and q["code_challenge_method"] == "S256"
        if self.deny_login:
            return RedirectResponse(
                f"{q['redirect_uri']}?{urlencode({'error': 'access_denied', 'state': q['state']})}"
            )
        code = secrets.token_urlsafe(16)
        self.codes[code] = {k: q[k] for k in ("code_challenge", "redirect_uri", "nonce")}
        return RedirectResponse(
            f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q['state']})}"
        )

    async def token(self, request: Request) -> Response:
        form = await request.form()
        grant = self.codes.pop(str(form.get("code")), None)  # single use
        if grant is None or form.get("client_secret") != self.client_secret:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        verifier = str(form.get("code_verifier"))
        if b64(hashlib.sha256(verifier.encode()).digest()) != grant["code_challenge"]:
            return JSONResponse({"error": "invalid_grant", "detail": "pkce"}, status_code=400)
        if form.get("redirect_uri") != grant["redirect_uri"]:
            return JSONResponse({"error": "invalid_grant", "detail": "redirect"}, status_code=400)
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": self.client_id,
            "iat": now,
            "exp": now + 300,
            "nonce": grant["nonce"],
            **self.account,
            **self.claim_overrides,
        }
        claims = {k: v for k, v in claims.items() if v is not None}
        headers = {"kid": self.kid, **self.header_overrides}
        id_token = jwt.encode(claims, self.signing_key, algorithm="RS256", headers=headers)
        return JSONResponse(
            {"id_token": id_token, "access_token": "unused", "token_type": "Bearer"}
        )
