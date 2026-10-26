# Observability

## Logs
`HIVE_LOG_FORMAT=json` writes one JSON object per line on stderr (`ts`, `level`, `logger`, `msg`, plus fields);
`text` (default) is human-readable. `HIVE_LOG_LEVEL` sets the level. Uvicorn's own lines use the same format.

Every response carries `X-Request-ID`. A well-formed incoming id (8-64 characters of `A-Za-z0-9._-`) is kept so a gateway's
trace id flows through; anything else is replaced. The id is on every log line written while handling the request.

The access log (`hive.access`, one line per request at INFO) holds method, **path without the query string**, status,
duration in ms, route group and client address. It never holds tokens, query strings, or request/response bodies.
Health and metrics scrapes are logged at DEBUG only. Security events use `hive.security` (`oauth.login`,
`oauth.login_denied`, `admin.login`, `groups.sync`, ...).

## Metrics
Set `HIVE_METRICS_TOKEN` to serve Prometheus metrics at `/metrics` (`Authorization: Bearer <token>`); without the
token the endpoint does not exist. Labels are bounded sets only (route group, tool name, job kind), never ids or paths.

| Metric | Meaning |
|---|---|
| `hive_http_requests_total{group,method,status}` / `hive_http_request_seconds` | traffic by route group: `mcp`, `oauth`, `admin`, `health`, `metrics`, `other` |
| `hive_tool_calls_total{tool,outcome}` / `hive_tool_seconds{tool}` | tool calls; outcome `ok`, `error` (expected: not found, denied, conflict, quota...) or `internal_error` (a bug) |
| `hive_jobs{kind,state}` | queued / running / **dead** background jobs (done jobs are not counted) |
| `hive_job_oldest_due_seconds{kind}` | how long the oldest runnable job has waited |
| `hive_vector_coverage_ratio`, `hive_vector_items_missing` | share of items embedded with the current model |
| `hive_build_info{version}` | running version |

Job and vector gauges are computed when scraped (two small queries), so an idle server does no extra work. With several
replicas each reports the same shared numbers; request and tool counters are per replica, so sum them.

Example alert rules: [`deploy/prometheus-alerts.yml`](../deploy/prometheus-alerts.yml) (dead jobs, job backlog, search index
behind, 5xx rate, unexpected tool errors, rate limiting, failed sign-ins, slow search).

## Looking into a problem
1. Take the `X-Request-ID` from the client (or the failing request's response) and search the logs for it.
2. `hive_tool_calls_total{outcome="internal_error"}` rising: the log line with `unexpected error in tool <name>` has the traceback.
3. Dead jobs: `hive_jobs{state="dead"}`; the job's last error is stored with it (`jobs.last_error`).
4. Search results look stale: check `hive_vector_coverage_ratio` and `hive vectors status`.
