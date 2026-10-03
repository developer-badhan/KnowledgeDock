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
#   * Both stages are python:3.12 on Debian trixie, so the venv's interpreter
#     symlink (/usr/local/bin/python3.12) resolves in the runtime image too.
#
# Required build context files: pyproject.toml, uv.lock, .python-version, src/
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1 — builder
# -----------------------------------------------------------------------------
# uv publishes `trixie` from 0.10.x onward; the last image with a `bookworm`
# variant was 0.9.30. Pin the version that generated uv.lock.
FROM ghcr.io/astral-sh/uv:0.11.16-python3.12-trixie-slim AS builder

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
# Same Debian release as the builder on purpose. Wheels with compiled extensions
# (pydantic-core, uvloop) are resolved against the builder's glibc; running them
# on an older base would fail at import time, not at build time.
FROM python:3.12-slim-trixie AS runtime

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
#
# Group and user are created explicitly rather than relying on adduser defaults:
# `adduser --system` does NOT create a same-named group (it assigns the primary
# group from /etc/default/useradd), so `chown appuser:appuser` would fail with
# "invalid group". Naming --gid removes that ambiguity. uid/gid 10001 sit
# outside the 0-999 system range on purpose, which also silences the
# SYS_UID_MAX warning.
#
# adduser is not used here: it is a perl wrapper that shells out to useradd,
# and both binaries ship in the python:slim base, so calling useradd directly
# skips a layer of indirection.
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid 10001 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin appuser \
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