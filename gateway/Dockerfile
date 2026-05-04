# syntax=docker/dockerfile:1.7

# ---- builder: uv resolves deps and installs into /app/.venv ----
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Pull uv as a standalone binary. No pip, no system Python pollution.
ADD https://astral.sh/uv/install.sh /install-uv.sh
RUN chmod 0755 /install-uv.sh \
    && /install-uv.sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv \
    && rm /install-uv.sh

WORKDIR /app

# Copy only the files uv needs to resolve deps. This layer is cached
# as long as pyproject.toml and uv.lock are unchanged, so source-only
# edits don't re-resolve.
# README.md is copied here because hatchling reads it per
# pyproject.toml's `readme = "README.md"` when building the wheel
# for fitt-gateway (happens in the second `uv sync` below). Missing
# it produces "OSError: Readme file does not exist: README.md".
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Now copy the source and install the project itself.
COPY src/ src/
RUN uv sync --frozen --no-dev


# ---- runtime: small, non-root, no uv ----
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FITT_HOME=/fitt \
    PATH=/app/.venv/bin:$PATH

# Non-root user matching the QNAP admin default (uid/gid 1000). The
# user is overridable at runtime via compose's `user: "$PUID:$PGID"`.
RUN groupadd --system --gid 1000 fitt \
    && useradd --system --uid 1000 --gid 1000 --home-dir /home/fitt --create-home fitt \
    && mkdir -p /fitt \
    && chown -R fitt:fitt /fitt

WORKDIR /app
COPY --from=builder --chown=fitt:fitt /app /app

USER fitt
EXPOSE 8080

# Self-healthcheck via /health. Used by compose's
# `depends_on: condition: service_healthy` so dependent containers
# (bot, open-webui) wait until the gateway can actually serve.
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request, sys; \
sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health', timeout=2).status == 200 else 1)"

ENTRYPOINT ["python", "-m", "gateway"]
