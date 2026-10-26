"""Logs, request ids and metrics, end to end and in pieces."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families

from hive_mcp.ops.logs import JsonFormatter, configure_logging, request_id_var
from hive_mcp.ops.metrics import Metrics
from hive_mcp.ops.observe import route_group
from tests.conformance.factory import ALICE
from tests.integration.conftest import Live


def samples(text: str) -> dict[tuple[str, tuple], float]:
    out = {}
    for family in text_string_to_metric_families(text):
        for s in family.samples:
            out[(s.name, tuple(sorted(s.labels.items())))] = s.value
    return out


def value(text: str, name: str, **labels: str) -> float:
    return samples(text).get((name, tuple(sorted(labels.items()))), 0.0)


@pytest.fixture
def metered(tmp_path):
    srv = Live(tmp_path / "data", metrics_token="scrape-secret")
    srv.start()
    yield srv
    srv.stop()


def scrape(live: Live, token: str = "scrape-secret") -> httpx.Response:  # noqa: S107
    return httpx.get(f"{live.url}/metrics", headers={"Authorization": f"Bearer {token}"})


# ---------------------------------------------------------------- /metrics access
def test_metrics_are_off_without_a_token(live: Live):
    assert httpx.get(f"{live.url}/metrics").status_code == 404


def test_metrics_need_the_scrape_token(metered: Live):
    assert httpx.get(f"{metered.url}/metrics").status_code == 401
    for bad in ("wrong", "scrape-secre", "scrape-secret-x"):
        assert scrape(metered, bad).status_code == 401
    assert scrape(metered).status_code == 200
    assert scrape(metered).headers["content-type"].startswith("text/plain")
    assert "Bearer" in httpx.get(f"{metered.url}/metrics").headers["www-authenticate"]


# ---------------------------------------------------------------- request metrics
def test_requests_are_counted_by_group_and_status(metered: Live):
    before = scrape(metered).text
    httpx.get(f"{metered.url}/healthz")
    httpx.post(f"{metered.url}/mcp", json={})  # no credentials
    after = scrape(metered).text
    assert value(after, "hive_http_requests_total", group="health", method="GET", status="200") == (
        value(before, "hive_http_requests_total", group="health", method="GET", status="200") + 1
    )
    assert value(after, "hive_http_requests_total", group="mcp", method="POST", status="401") >= 1
    assert value(after, "hive_http_request_seconds_count", group="health") >= 1


async def test_tool_calls_and_backlog_are_reported(metered: Live):
    before = scrape(metered).text
    async with metered.client(metered.token_for(ALICE)) as client:
        await client.call_tool("kb_remember", {"title": "Metrics note", "body": "Counted."})
        await client.call_tool("kb_get", {"id": "missing"})
    text = scrape(metered).text

    def grew(
        name: str, **labels: str
    ) -> float:  # the registry is process-wide: compare, don't total
        return value(text, name, **labels) - value(before, name, **labels)

    assert grew("hive_tool_calls_total", tool="kb_remember", outcome="ok") == 1
    assert grew("hive_tool_calls_total", tool="kb_get", outcome="error") == 1
    assert grew("hive_tool_seconds_count", tool="kb_remember") == 1
    ratio = value(text, "hive_vector_coverage_ratio")
    assert 0.0 <= ratio <= 1.0
    assert ("hive_build_info", (("version", __import__("hive_mcp").__version__),)) in samples(text)


def test_metric_labels_never_contain_paths_or_ids(metered: Live):
    httpx.get(f"{metered.url}/no/such/path/with-an-id-12345")
    text = scrape(metered).text
    assert "12345" not in text and "no/such" not in text
    assert value(text, "hive_http_requests_total", group="other", method="GET", status="404") >= 1


def test_route_groups():
    assert route_group("/mcp", "/mcp") == "mcp" and route_group("/mcp/x", "/mcp") == "mcp"
    assert (
        route_group("/token", "/mcp") == "oauth" and route_group("/admin/audit", "/mcp") == "admin"
    )
    assert route_group("/.well-known/oauth-authorization-server", "/mcp") == "oauth"
    assert (
        route_group("/readyz", "/mcp") == "health" and route_group("/metrics", "/mcp") == "metrics"
    )
    assert route_group("/other", "/mcp") == "other"


async def test_job_gauges_follow_the_queue():
    from hive_mcp.adapters.memory import MemoryBackend  # noqa: PLC0415

    backend = MemoryBackend()
    await backend.jobs.enqueue("o", "embed", {})
    backend.clock.advance(seconds=42)

    class Services:
        class vector_admin:
            @staticmethod
            async def status():
                from hive_mcp.services.vector_admin import VectorStatus  # noqa: PLC0415

                return VectorStatus("m", 3, 4, {})

    Services.backend = backend  # type: ignore[attr-defined]
    metrics = Metrics()
    await metrics.refresh(Services)  # type: ignore[arg-type]
    text = metrics.render().decode()
    assert value(text, "hive_jobs", kind="embed", state="queued") == 1
    assert value(text, "hive_job_oldest_due_seconds", kind="embed") == 42
    assert value(text, "hive_vector_coverage_ratio") == 0.75
    assert value(text, "hive_vector_items_missing") == 1


# ---------------------------------------------------------------- request ids and logs
def test_request_ids_are_echoed_when_sane_and_replaced_when_not(live: Live):
    ok = httpx.get(f"{live.url}/healthz", headers={"X-Request-ID": "trace-abc-12345"})
    assert ok.headers["x-request-id"] == "trace-abc-12345"
    for bad in ("short", "has spaces in it!!", "x" * 100, "inj\tect"):
        got = httpx.get(f"{live.url}/healthz", headers={"X-Request-ID": bad}).headers[
            "x-request-id"
        ]
        assert got != bad and len(got) == 32
    assert httpx.get(f"{live.url}/healthz").headers["x-request-id"]


def test_access_log_line_has_the_request_facts_and_no_secrets(live: Live, caplog):
    caplog.set_level(logging.DEBUG, logger="hive.access")
    httpx.post(
        f"{live.url}/mcp?secret=in-query",
        json={},
        headers={"Authorization": "Bearer hive_supersecrettoken", "X-Request-ID": "req-123456"},
    )
    (record,) = [r for r in caplog.records if r.name == "hive.access" and r.path == "/mcp"]
    assert (record.method, record.status, record.group) == ("POST", 401, "mcp")
    assert record.ms >= 0
    blob = json.dumps(record.__dict__, default=str)
    assert "supersecret" not in blob and "in-query" not in blob


def test_quiet_routes_log_at_debug_only(live: Live, caplog):
    caplog.set_level(logging.INFO, logger="hive.access")
    httpx.get(f"{live.url}/healthz")
    assert not [r for r in caplog.records if r.name == "hive.access"]


# ---------------------------------------------------------------- json logging
def test_json_lines_carry_the_request_id_extras_and_exceptions():
    record = logging.LogRecord("hive.x", logging.WARNING, "f", 1, "hello %s", ("world",), None)
    record.status = 429
    token = request_id_var.set("rid-1")
    try:
        entry = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert (
        entry["msg"] == "hello world"
        and entry["level"] == "warning"
        and entry["logger"] == "hive.x"
    )
    assert (
        entry["request_id"] == "rid-1" and entry["status"] == 429 and entry["ts"].endswith("+00:00")
    )
    try:
        raise ValueError("boom")
    except ValueError:
        import sys  # noqa: PLC0415

        failing = logging.LogRecord("x", logging.ERROR, "f", 1, "bad", (), sys.exc_info())
    assert "ValueError: boom" in json.loads(JsonFormatter().format(failing))["exc"]
    assert "request_id" not in json.loads(JsonFormatter().format(record))  # outside a request


def test_configure_logging_replaces_its_own_handler_only(capsys):
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    try:
        configure_logging("json", "info")
        configure_logging("json", "warning")
        ours = [h for h in root.handlers if h.get_name() == "hive"]
        assert len(ours) == 1 and root.level == logging.WARNING
        logging.getLogger("hive.t").warning("structured")
        assert json.loads(capsys.readouterr().err.strip().splitlines()[-1])["msg"] == "structured"
        configure_logging("text", "info")
        assert not isinstance(
            next(h for h in root.handlers if h.get_name() == "hive").formatter, JsonFormatter
        )
    finally:
        root.handlers[:], root.level = saved


async def test_stale_job_gauges_disappear_when_the_queue_drains():
    from hive_mcp.adapters.memory import MemoryBackend  # noqa: PLC0415
    from hive_mcp.services.vector_admin import VectorStatus  # noqa: PLC0415

    backend = MemoryBackend()
    job = await backend.jobs.enqueue("o", "embed", {})

    class Admin:
        @staticmethod
        async def status():
            return VectorStatus("m", 1, 1, {})

    class Services:
        vector_admin = Admin

    Services.backend = backend  # type: ignore[attr-defined]
    metrics = Metrics()
    await metrics.refresh(Services)  # type: ignore[arg-type]
    assert value(metrics.render().decode(), "hive_jobs", kind="embed", state="queued") == 1
    await backend.jobs.claim("w", frozenset({"embed"}), 30)
    await backend.jobs.complete(job.id, "w")
    await metrics.refresh(Services)  # type: ignore[arg-type]
    assert 'kind="embed"' not in metrics.render().decode()


async def test_tool_outcomes_distinguish_expected_and_unexpected_failures():
    from mcp.server.mcpserver.exceptions import ToolError  # noqa: PLC0415

    from hive_mcp.domain.errors import NotFound  # noqa: PLC0415
    from hive_mcp.mcp_api.errors import guarded  # noqa: PLC0415
    from hive_mcp.ops.metrics import METRICS  # noqa: PLC0415

    @guarded
    async def probe_ok() -> int:
        return 1

    @guarded
    async def probe_user() -> int:
        raise NotFound("x")

    @guarded
    async def probe_bug() -> int:
        raise RuntimeError("boom")

    await probe_ok()
    for fn in (probe_user, probe_bug):
        with pytest.raises(ToolError):
            await fn()
    text = METRICS.render().decode()
    assert value(text, "hive_tool_calls_total", tool="probe_ok", outcome="ok") >= 1
    assert value(text, "hive_tool_calls_total", tool="probe_user", outcome="error") >= 1
    assert value(text, "hive_tool_calls_total", tool="probe_bug", outcome="internal_error") >= 1
    assert value(text, "hive_tool_calls_total", tool="probe_bug", outcome="error") == 0
