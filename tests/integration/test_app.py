import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from hive_mcp.ops.health import health_routes
from hive_mcp.ops.state import Lifecycle
from tests.integration.conftest import Live


def test_health_endpoints_need_no_credentials(live: Live) -> None:
    assert httpx.get(f"{live.url}/healthz").status_code == 200
    assert httpx.get(f"{live.url}/readyz").status_code == 200


def test_readyz_flips_to_503_when_draining(live: Live) -> None:
    live.lifecycle.start_draining()
    assert httpx.get(f"{live.url}/readyz").status_code == 503


def test_readiness_is_503_until_services_are_built() -> None:
    lifecycle = Lifecycle()
    client = TestClient(Starlette(routes=health_routes(lifecycle)))
    assert client.get("/readyz").status_code == 503 and client.get("/healthz").status_code == 200
    lifecycle.mark_ready()
    assert client.get("/readyz").status_code == 200
    _ = pytest
