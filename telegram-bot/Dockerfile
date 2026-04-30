# syntax=docker/dockerfile:1.7
#
# Build context for this image is the repo root, NOT ./telegram-bot.
# The bot depends on fitt-gateway via a local path source
# (see telegram-bot/pyproject.toml: tool.uv.sources), so uv needs
# the ../gateway tree visible at build time. Compose invokes this
# with `context: .` and `dockerfile: telegram-bot/Dockerfile`.

# ---- builder ----
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ADD https://astral.sh/uv/install.sh /install-uv.sh
RUN chmod 0755 /install-uv.sh \
    && /install-uv.sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv \
    && rm /install-uv.sh

WORKDIR /app

# uv needs to see the gateway package (via tool.uv.sources path dep)
# AND the bot package. Copy both trees into the builder, preserving
# the relative layout so pyproject.toml's `../gateway` still resolves.
COPY gateway/      /app/gateway/
COPY telegram-bot/ /app/telegram-bot/

WORKDIR /app/telegram-bot
RUN uv sync --frozen --no-dev


# ---- runtime ----
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FITT_HOME=/fitt \
    PATH=/app/telegram-bot/.venv/bin:$PATH

RUN groupadd --system --gid 1000 fitt \
    && useradd --system --uid 1000 --gid 1000 --home-dir /home/fitt --create-home fitt \
    && mkdir -p /fitt \
    && chown -R fitt:fitt /fitt

WORKDIR /app
# Copy both trees so editable-install paths inside the venv remain
# valid at runtime.
COPY --from=builder --chown=fitt:fitt /app /app

USER fitt

# No ports exposed: the bot only makes outbound connections
# (api.telegram.org long poll + http://gateway:8080).
# No HEALTHCHECK: "bot is healthy" means "bot is polling", which
# isn't easily probed from outside. Compose's restart policy
# handles crashes.

ENTRYPOINT ["python", "-m", "fitt_telegram_bot"]
