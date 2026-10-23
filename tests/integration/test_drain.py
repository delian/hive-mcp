import time

import httpx

from tests.integration.conftest import Live


def test_readiness_fails_during_grace_then_server_exits() -> None:
    srv = Live(grace=1.0)
    srv.start()
    assert httpx.get(f"{srv.url}/readyz").status_code == 200
    srv.server.handle_exit(15, None)
    time.sleep(0.2)
    assert httpx.get(f"{srv.url}/readyz").status_code == 503
    assert httpx.get(f"{srv.url}/healthz").status_code == 200  # still served while draining
    srv.thread.join(timeout=10)
    assert not srv.thread.is_alive()


def test_second_signal_exits_immediately() -> None:
    srv = Live(grace=30.0)
    srv.start()
    srv.server.handle_exit(15, None)
    srv.server.handle_exit(15, None)
    srv.thread.join(timeout=10)
    assert not srv.thread.is_alive()
