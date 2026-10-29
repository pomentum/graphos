# syntax=docker/dockerfile:1.7
# Build the graphos-api service.
# Two-stage: build wheels in stage 1, slim runtime in stage 2.

# Optional extras to bake in, comma-separated. Empty by default so the image
# contains only permissively licensed code. Pass GRAPHOS_EXTRAS=louvain to add
# Louvain community detection (python-igraph, GPL-2.0-or-later): an image
# built that way includes GPL code, so redistribute it accordingly.
ARG GRAPHOS_EXTRAS=""

FROM python:3.11-slim AS builder
ARG GRAPHOS_EXTRAS

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Install build deps for asyncpg (libpq is statically built into the wheel; gcc only needed if no wheel)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
COPY src/ ./src/

# Build the graphos wheel + collect every runtime dep transitively.
# Single `pip wheel` call: the wheel for graphos itself drives discovery of
# runtime deps (fastapi, asyncpg, …). No hardcoded duplicate list — pyproject
# is the single source of truth.
RUN pip install --upgrade pip build && \
    python -m build --wheel --outdir /wheels && \
    whl="$(ls /wheels/graphos-*.whl)" && \
    pip wheel --wheel-dir /wheels "${whl}${GRAPHOS_EXTRAS:+[$GRAPHOS_EXTRAS]}"

# ── Runtime stage ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime
ARG GRAPHOS_EXTRAS

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Non-root user for runtime
RUN groupadd --system --gid 1001 graphos && \
    useradd  --system --uid 1001 --gid graphos --no-create-home --shell /usr/sbin/nologin graphos

WORKDIR /app

COPY --from=builder /wheels /wheels
# Licence texts travel with the image (Apache-2.0 + third-party notices).
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /usr/share/licenses/graphos/
RUN pip install --no-cache-dir --no-index --find-links /wheels "graphos${GRAPHOS_EXTRAS:+[$GRAPHOS_EXTRAS]}" && \
    rm -rf /wheels
# Installs python-igraph at start when GRAPHOS_IGRAPH=true, then runs graphos.
COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/graphos-entrypoint

USER graphos

EXPOSE 8080

# The entrypoint ends in `python -m graphos` (the CLI shim in src/graphos/__main__.py),
# so signal handling and lifespan startup follow the same path as before.
ENTRYPOINT ["graphos-entrypoint"]
