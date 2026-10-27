"""RFC 7009 revocation: who may revoke what, and the exact answers."""

from __future__ import annotations

import pytest
from mcp.server.auth.provider import AccessToken, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull
from starlette.applications import Starlette
from starlette.testclient import TestClient

from hive_mcp.security.oauth.models import ACCESS_PREFIX
from hive_mcp.security.oauth.revoke import MAX_FORM_BYTES, revoke_route


def info(client_id: str) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id, redirect_uris=["https://c.example/cb"],
        token_endpoint_auth_method="none",
    )  # fmt: skip


class Provider:
    def __init__(self) -> None:
        self.clients = {"mine": info("mine"), "other": info("other")}
        self.access = {ACCESS_PREFIX + "a1": "mine", ACCESS_PREFIX + "theirs": "other"}
        self.refresh = {"hrt_r1": "mine", "hrt_theirs": "other"}
        self.revoked: list[str] = []

    async def get_client(self, client_id: str):
        return self.clients.get(client_id)

    async def load_access_token(self, token: str):
        owner = self.access.get(token)
        return AccessToken(token=token, client_id=owner, scopes=[]) if owner else None

    async def load_refresh_token(self, client, token: str):
        owner = self.refresh.get(token)
        return RefreshToken(token=token, client_id=owner, scopes=[]) if owner else None

    async def revoke_token(self, token) -> None:
        self.revoked.append(token.token)


@pytest.fixture
def world():
    provider = Provider()
    app = Starlette(routes=[revoke_route(provider)])  # type: ignore[arg-type]
    return TestClient(app), provider


def test_a_client_revokes_its_own_access_and_refresh_tokens(world, caplog):
    client, provider = world
    with caplog.at_level("INFO", logger="hive.security"):
        for token in (ACCESS_PREFIX + "a1", "hrt_r1"):
            reply = client.post("/revoke", data={"client_id": "mine", "token": token})
            assert reply.status_code == 200 and reply.headers["cache-control"] == "no-store"
            assert reply.content == b""
    assert provider.revoked == [ACCESS_PREFIX + "a1", "hrt_r1"]
    assert [r.getMessage() for r in caplog.records] == ["oauth.revoked client=mine"] * 2


def test_other_clients_and_unknown_tokens_get_the_same_answer_and_revoke_nothing(world):
    client, provider = world
    for token in (ACCESS_PREFIX + "theirs", "hrt_theirs", "hrt_unknown", ACCESS_PREFIX + "gone"):
        reply = client.post("/revoke", data={"client_id": "mine", "token": token})
        assert reply.status_code == 200 and reply.headers["cache-control"] == "no-store"
    assert provider.revoked == []


def test_a_request_without_a_token_or_a_known_client_is_refused_with_the_standard_errors(world):
    client, _ = world
    missing = client.post("/revoke", data={"client_id": "mine"})
    assert missing.status_code == 400
    assert missing.json() == {"error": "invalid_request", "error_description": "token is required"}
    blank = client.post("/revoke", data={"client_id": "mine", "token": ""})
    assert blank.status_code == 400
    unknown = client.post("/revoke", data={"client_id": "stranger", "token": "x"})
    assert unknown.status_code == 401
    body = unknown.json()
    assert body["error"] == "unauthorized_client" and body["error_description"]
    anonymous = client.post("/revoke", data={"token": "x"})
    assert anonymous.status_code == 401 and anonymous.json()["error"] == "unauthorized_client"


def test_the_declared_size_is_checked_before_anything_is_read(world):
    client, provider = world

    def post(length: str):
        return client.post(
            "/revoke", content=b"client_id=mine&token=hrt_r1",
            headers={"content-type": "application/x-www-form-urlencoded", "content-length": length},
        )  # fmt: skip

    for bad in ("abc", "-1", "1.5", "9" * 30, str(MAX_FORM_BYTES + 1)):
        reply = post(bad)
        assert reply.status_code == 413 and reply.json() == {"error": "invalid_request"}, bad
    assert provider.revoked == []


def test_exactly_the_limit_is_allowed():
    provider = Provider()
    client = TestClient(Starlette(routes=[revoke_route(provider)]))  # type: ignore[arg-type]
    prefix = b"client_id=mine&token=hrt_r1&pad="
    body = prefix + b"x" * (MAX_FORM_BYTES - len(prefix))
    assert len(body) == MAX_FORM_BYTES
    reply = client.post(
        "/revoke", content=body, headers={"content-type": "application/x-www-form-urlencoded"}
    )
    assert reply.status_code == 200 and provider.revoked == ["hrt_r1"]


def test_a_body_without_a_declared_length_is_treated_as_empty_not_as_an_error(world):
    client, _ = world

    def chunks():
        yield b"client_id=mine&token=hrt_r1"

    reply = client.post(
        "/revoke", content=chunks(), headers={"content-type": "application/x-www-form-urlencoded"}
    )
    assert reply.status_code == 200  # no content-length: nothing to compare, the form is read


async def test_a_non_ascii_digit_in_the_length_header_is_a_refusal_not_a_crash():
    from starlette.requests import Request

    route = revoke_route(Provider())  # type: ignore[arg-type]
    scope = {
        "type": "http", "method": "POST", "path": "/revoke", "query_string": b"",
        "headers": [(b"content-length", "²".encode("latin-1"))],
    }  # fmt: skip
    reply = await route.endpoint(Request(scope))
    assert reply.status_code == 413
