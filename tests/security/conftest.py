from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from hive_mcp.adapters.memory.kv import MemoryKv
from hive_mcp.domain.clock import FixedClock
from hive_mcp.domain.principal import Role
from hive_mcp.security.directory import DirectoryUser, UserDirectory
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.security.oauth.models import OAuthConfig
from hive_mcp.security.oauth.provider import HiveOAuthProvider

if TYPE_CHECKING:
    from pathlib import Path

REDIRECT = "https://client.example/callback"
RESOURCE = "https://hive.example/mcp"


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


@dataclass
class Rig:
    provider: HiveOAuthProvider
    clock: FixedClock
    directory: UserDirectory
    client: OAuthClientInformationFull

    async def new_client(self, name: str = "other") -> OAuthClientInformationFull:
        client = make_client(name)
        await self.provider.register_client(client)
        return client

    async def code_for(
        self, email: str = "alice@acme.com", client: OAuthClientInformationFull | None = None
    ) -> str:
        client = client or self.client
        params = AuthorizationParams(
            state="st",
            scopes=None,
            code_challenge="c" * 43,
            redirect_uri=AnyUrl(REDIRECT),
            redirect_uri_provided_explicitly=True,
            resource=None,
        )
        txn_id = query(await self.provider.authorize(client, params))["txn"]
        txn = await self.provider.load_txn(txn_id)
        await self.provider.attach_user(txn_id, txn, email)
        return query(await self.provider.finish(txn_id, approve=True))["code"]

    async def tokens(self, email: str = "alice@acme.com"):
        code = await self.provider.load_authorization_code(self.client, await self.code_for(email))
        return await self.provider.exchange_authorization_code(self.client, code)


def make_client(name: str = "app") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=f"client-{name}",
        client_name=name,
        redirect_uris=[AnyUrl(REDIRECT)],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="hive",
    )


@pytest.fixture
async def rig(tmp_path: Path) -> Rig:
    clock = FixedClock()
    kv = MemoryKv(clock)
    directory = UserDirectory(AtomicJsonFile(tmp_path / "users.json", default=[]))
    directory.upsert(
        DirectoryUser("alice@acme.com", "acme", Role.MEMBER, frozenset({"t1"}), frozenset({"p1"}))
    )
    directory.upsert(DirectoryUser("bob@acme.com", "acme", Role.EDITOR, projects=frozenset({"p1"})))
    provider = HiveOAuthProvider(
        get_kv=lambda: kv,
        directory=directory,
        clock=clock,
        config=OAuthConfig(
            "https://hive.example", RESOURCE, access_ttl=3600, refresh_ttl=30 * 86400
        ),
    )
    client = make_client()
    await provider.register_client(client)
    return Rig(provider, clock, directory, client)
