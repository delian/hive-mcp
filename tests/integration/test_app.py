import httpx
import pytest
from httpx import ASGITransport

from hive_mcp.app import create_app
from hive_mcp.config import Settings
from hive_mcp.ops.state import Lifecycle


@pytest.fixture
def lifecycle() -> Lifecycle:
    return Lifecycle()


@pytest.fixture
async def client(lifecycle: Lifecycle) -> httpx.AsyncClient:
    app = create_app(Settings(), lifecycle)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_healthz(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200


async def test_readyz_flips_to_503_when_draining(
    client: httpx.AsyncClient, lifecycle: Lifecycle
) -> None:
    assert (await client.get("/readyz")).status_code == 200
    lifecycle.start_draining()
    assert (await client.get("/readyz")).status_code == 503
