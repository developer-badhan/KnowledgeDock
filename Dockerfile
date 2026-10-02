# syntax=docker/dockerfile:1.7

# =============================================================================
# KnowledgeDock — multi-stage image
# =============================================================================
# Stage 1 (builder): compiles bytecode and installs deps into /app/.venv with uv.
# Stage 2 (runtime): slim image, no compiler, no uv, non-root, only the venv.
#
# Layer caching strategy:
#   * pyproject.toml / uv.lock / .python-version are copied and installed FIRST,
#     on their own. Source code is copied after, so editing application code
#     reinstalls nothing.
#   * `--mount=type=cache` keeps uv's download cache outside the image layers.
#   * `uv sync --frozen` requires uv.lock to exist and never re-resolves deps.
#   * Tests and dev dependencies are excluded from the runtime image.
#
# Required build context files: pyproject.toml, uv.lock, .python-version, src/
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1 — builder
# -----------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:0.11.16-python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_DEV=1

WORKDIR /app

# Dependency layer — changes only when the manifests change.
# Dependencies only: the project itself is not installed into the venv, it is
# imported from /app/src via PYTHONPATH. Editing code therefore reinstalls nothing.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Application layer.
COPY src ./src


# -----------------------------------------------------------------------------
# Stage 2 — runtime
# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    PATH="/app/.venv/bin:$PATH" \
    PORT=8000 \
    HOST=0.0.0.0 \
    STORAGE_DIR=/tmp/knowledgedock/uploads

WORKDIR /app

# curl is only here for the container healthcheck below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user. Render overrides $PORT via its own env var.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p "$STORAGE_DIR" \
    && chown -R appuser:appuser "$STORAGE_DIR"

COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --chown=appuser:appuser src ./src

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# Must bind 0.0.0.0 and honour $PORT (Render sets PORT to 10000 by default).
CMD ["sh", "-c", "uvicorn knowledgedock.main:app --host ${HOST:-0.0.0.0} --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]