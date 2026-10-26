"""Prometheus metrics. Labels are bounded sets (route group, tool name, job kind/state), never ids,
paths or users, so cardinality stays small and nothing sensitive reaches the metrics system."""

from __future__ import annotations

import contextlib
import hmac
from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from hive_mcp import __version__
from hive_mcp.domain.errors import HiveError

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request

    from hive_mcp.bootstrap import Services

BUCKETS = (0.005, 0.025, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        reg = self.registry
        self.requests = Counter(
            "hive_http_requests_total", "HTTP requests", ["group", "method", "status"], registry=reg
        )
        self.request_seconds = Histogram(
            "hive_http_request_seconds",
            "HTTP request time",
            ["group"],
            buckets=BUCKETS,
            registry=reg,
        )
        self.tool_calls = Counter(
            "hive_tool_calls_total", "MCP tool calls", ["tool", "outcome"], registry=reg
        )
        self.tool_seconds = Histogram(
            "hive_tool_seconds", "MCP tool time", ["tool"], buckets=BUCKETS, registry=reg
        )
        self.jobs = Gauge(
            "hive_jobs", "Background jobs not yet done", ["kind", "state"], registry=reg
        )
        self.job_age = Gauge(
            "hive_job_oldest_due_seconds",
            "Age of the oldest runnable queued job",
            ["kind"],
            registry=reg,
        )
        self.coverage = Gauge(
            "hive_vector_coverage_ratio", "Items embedded with the current model", registry=reg
        )
        self.items_pending = Gauge(
            "hive_vector_items_missing", "Items still lacking a current vector", registry=reg
        )
        Gauge("hive_build_info", "Version", ["version"], registry=reg).labels(__version__).set(1)

    def observe_request(self, group: str, method: str, status: int, seconds: float) -> None:
        self.requests.labels(group, method, str(status)).inc()
        self.request_seconds.labels(group).observe(seconds)

    def observe_tool(self, tool: str, outcome: str, seconds: float) -> None:
        self.tool_calls.labels(tool, outcome).inc()
        self.tool_seconds.labels(tool).observe(seconds)

    async def refresh(self, services: Services) -> None:
        """Gauges that need the database; computed when scraped, so an idle server does no work."""
        self.jobs.clear()
        self.job_age.clear()
        for stat in await services.backend.jobs.stats():
            self.jobs.labels(stat.kind, str(stat.state)).set(stat.count)
            if stat.oldest_due_age:
                self.job_age.labels(stat.kind).set(stat.oldest_due_age)
        status = await services.vector_admin.status()
        self.coverage.set(status.coverage)
        self.items_pending.set(max(status.indexable_items - status.current_items, 0))

    def render(self) -> bytes:
        return generate_latest(self.registry)


METRICS = Metrics()


def metrics_routes(
    token: str | None, metrics: Metrics, get_services: Callable[[], Services]
) -> list[Route]:
    """Served only when a scrape token is configured; the endpoint is not public information."""
    if not token:
        return []
    secret = token.encode()

    async def scrape(request: Request) -> Response:
        header = request.headers.get("authorization", "")
        given = header[7:].encode() if header.lower().startswith("bearer ") else b""
        if not hmac.compare_digest(given, secret):
            return PlainTextResponse(
                "unauthorized", 401, headers={"WWW-Authenticate": 'Bearer realm="metrics"'}
            )
        with contextlib.suppress(HiveError):  # still serve request metrics while the store is busy
            await metrics.refresh(get_services())
        return Response(metrics.render(), media_type=CONTENT_TYPE)

    return [Route("/metrics", scrape)]
