# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM python:3.12-slim
RUN useradd --system --uid 10001 --create-home hive && mkdir /data && chown hive /data
COPY --from=build --chown=hive /app /app
ENV PATH="/app/.venv/bin:$PATH" HIVE_DATA_DIR=/data HIVE_PORT=8080 PYTHONUNBUFFERED=1
USER hive
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"HIVE_PORT\"]}/healthz')"
STOPSIGNAL SIGTERM
CMD ["hive", "serve"]
