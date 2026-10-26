# syntax=docker/dockerfile:1
# One image for local use and Azure. Build args:
#   EXTRAS     optional dependency groups to install (default: all runtime extras)
#   PYTHON_IMAGE base image (e.g. a registry mirror)
#   PG_CLIENT  install pg_dump/pg_restore for backups of the Postgres profile (default true)
#   BAKE_MODEL download + verify the offline embedding model into the image (needs network at build time)
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE} AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
ARG EXTRAS="--extra postgres --extra local-embeddings --extra documents"
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
# Behind a TLS-inspecting proxy, build with: --secret id=cacert,src=/path/to/ca-bundle.crt
RUN --mount=type=cache,target=/root/.cache/uv --mount=type=secret,id=cacert,required=false \
    if [ -f /run/secrets/cacert ]; then export SSL_CERT_FILE=/run/secrets/cacert UV_SYSTEM_CERTS=1; fi; \
    uv sync --frozen --no-dev --no-install-project $EXTRAS
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv --mount=type=secret,id=cacert,required=false \
    if [ -f /run/secrets/cacert ]; then export SSL_CERT_FILE=/run/secrets/cacert UV_SYSTEM_CERTS=1; fi; \
    uv sync --frozen --no-dev --no-editable $EXTRAS
ARG BAKE_MODEL=false
RUN --mount=type=secret,id=cacert,required=false \
    mkdir -p /models; \
    if [ -f /run/secrets/cacert ]; then export SSL_CERT_FILE=/run/secrets/cacert; fi; \
    if [ "$BAKE_MODEL" = "true" ]; then \
      HIVE_DATA_DIR=/tmp/x HIVE_MODELS_DIR=/models /app/.venv/bin/hive models fetch; fi

FROM ${PYTHON_IMAGE}
# pg_dump/pg_restore for `hive backup` on the Postgres profile (must be at least the server's major version)
ARG PG_CLIENT=true
RUN if [ "$PG_CLIENT" = "true" ]; then apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client \
 && rm -rf /var/lib/apt/lists/*; fi
RUN useradd --system --uid 10001 --create-home hive && mkdir /data && chown hive /data
COPY --from=build /app/.venv /app/.venv
COPY --from=build /models /models
ENV PATH="/app/.venv/bin:$PATH" HIVE_DATA_DIR=/data HIVE_MODELS_DIR=/models HIVE_PORT=8080 PYTHONUNBUFFERED=1
USER hive
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"HIVE_PORT\"]}/healthz')"
STOPSIGNAL SIGTERM
CMD ["hive", "serve"]
