# Phase 3.5 — Docker-first Hub: Design

## Topology

```
+------------------- Hub host (QNAP, Linux, Mac, Windows + Docker) -------------------+
|                                                                                     |
|    +-----------------------+        +--------------------------+                    |
|    |  fitt-gateway:local   |        |  fitt-telegram-bot:local |                    |
|    |  uvicorn on :8080     |<-------|  long-poll Telegram API  |                    |
|    |  reads /fitt/*.yaml   |        |  reads /fitt/*.yaml      |                    |
|    +-----------+-----------+        +--------------------------+                    |
|                |                                                                    |
|                |        +--------------------------------+                          |
|                +------->| ghcr.io/open-webui/open-webui  |                          |
|                         |    (pinned tag)                |                          |
|                         +--------------------------------+                          |
|                                                                                     |
|    Bind mount:   ${FITT_HOME}  ->  /fitt    (config, secrets, sessions, logs)       |
|    Named vol:    open-webui-data           (chat history, admin account)            |
|                                                                                     |
|    Published ports:  8080 (gateway), 3000 (Open WebUI)                              |
|                                                                                     |
+-------------------------------------------------------------------------------------+

                                            v  (outbound only)
              Tailscale: satellites (Ollama), phone, laptop, api.telegram.org, OpenRouter
```

Four principles drive the design:

1. **Three images, one compose file.** One image per language/runtime
   concern. Compose orchestrates. No supervisord-in-one-image.
2. **Stateful data lives in bind mounts.** `config.yaml`,
   `secrets.yaml`, `sessions/`, `identity/`, `logs/` all live on the
   host under a single `FITT_HOME` directory. Containers read and
   write there. Blow the containers away, data persists.
3. **The gateway URL inside the compose network is
   `http://gateway:8080`.** Services address each other by name, not
   by IP or host port. Only `gateway:8080` and `open-webui:3000`
   publish to the host.
4. **No secrets in the image.** Images are safe to push to a registry
   if we ever do. Secrets enter via bind-mounted files at runtime.

## Repository layout

Changes are additive; nothing existing moves.

```
home-ai-cluster/
  docker-compose.yml                   # rewritten to own all three services
  docker-compose.override.yml.example  # dev overlay (hot-reload)
  .env.example                         # compose vars (FITT_HOME, PUID, PGID)

  gateway/
    Dockerfile                         # NEW
    .dockerignore                      # NEW
    ...existing code unchanged...

  telegram-bot/
    Dockerfile                         # NEW
    .dockerignore                      # NEW
    ...existing code unchanged...

  scripts/
    install-service.ps1                # unchanged; Windows path
    install-telegram-bot.ps1           # unchanged; Windows path
    install-open-webui.ps1             # REMOVED (compose covers this)
    uninstall-open-webui.ps1           # REMOVED
```

## Dockerfiles

### Gateway

Multi-stage build: a `builder` stage runs `uv sync` into a venv at
`/app/.venv`, a thin runtime stage copies that venv plus the source.
This keeps the final image small and avoids shipping uv itself.

```dockerfile
# syntax=docker/dockerfile:1.7

# ---- builder: resolve + install deps ----
FROM python:3.11-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ADD --chmod=0755 https://astral.sh/uv/install.sh /install-uv.sh
RUN /install-uv.sh && ln -s /root/.local/bin/uv /usr/local/bin/uv
WORKDIR /app
# Copy only the files uv needs to resolve first, for layer cache
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# Now copy the source and install the project itself
COPY src/ src/
RUN uv sync --frozen --no-dev

# ---- runtime: small, non-root, no uv ----
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FITT_HOME=/fitt \
    PATH=/app/.venv/bin:$PATH
RUN groupadd -g 1000 fitt && useradd -u 1000 -g 1000 -m fitt \
    && mkdir -p /fitt && chown fitt:fitt /fitt
WORKDIR /app
COPY --from=builder --chown=fitt:fitt /app /app
USER fitt
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request, sys; sys.exit(0) if urllib.request.urlopen('http://localhost:8080/health', timeout=2).status == 200 else sys.exit(1)"
ENTRYPOINT ["python", "-m", "gateway"]
```

The image is ~120 MB uncompressed. Build time on a TS-253Be is
30-60 seconds cold, under 10 seconds with cached dep layers.

`PUID` / `PGID` handling (R2 in requirements): the default user is
`fitt:fitt` with uid/gid 1000. If the host's `FITT_HOME` is owned
by a different uid, we do NOT run a gosu shim to re-exec; we
document that the user should ensure the bind-mount directory is
owned by 1000:1000, OR they set `user: "$PUID:$PGID"` in the
compose file. Keeping the image free of gosu means no entrypoint
shell script and fewer moving parts. QNAP admins are 1000 by
default, so zero-config works for the common case.

### Telegram bot

Same pattern, smaller concerns. No port exposure, no healthcheck
(bot health is "still polling", validated by the gateway's own
metrics).

```dockerfile
FROM python:3.11-slim AS builder
# ... same uv-based install ...

FROM python:3.11-slim
ENV FITT_HOME=/fitt PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
RUN groupadd -g 1000 fitt && useradd -u 1000 -g 1000 -m fitt \
    && mkdir -p /fitt && chown fitt:fitt /fitt
WORKDIR /app
COPY --from=builder --chown=fitt:fitt /app /app
USER fitt
ENTRYPOINT ["python", "-m", "fitt_telegram_bot"]
```

## docker-compose.yml

```yaml
# Compose v2 - no version key needed on modern Docker.
services:
  gateway:
    build:
      context: ./gateway
      dockerfile: Dockerfile
    image: fitt-gateway:local
    container_name: fitt-gateway
    restart: unless-stopped
    user: "${PUID:-1000}:${PGID:-1000}"
    environment:
      FITT_HOME: /fitt
      FITT_CONFIG_PATH: /fitt/config.yaml
      FITT_SECRETS_PATH: /fitt/secrets.yaml
    volumes:
      - ${FITT_HOME:-./fitt-data}:/fitt
    ports:
      - "8080:8080"
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"

  telegram-bot:
    build:
      context: ./telegram-bot
      dockerfile: Dockerfile
    image: fitt-telegram-bot:local
    container_name: fitt-telegram-bot
    restart: unless-stopped
    user: "${PUID:-1000}:${PGID:-1000}"
    depends_on:
      gateway:
        condition: service_healthy
    environment:
      FITT_HOME: /fitt
      FITT_GATEWAY_URL: http://gateway:8080
    volumes:
      - ${FITT_HOME:-./fitt-data}:/fitt
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"

  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    container_name: fitt-open-webui
    restart: unless-stopped
    depends_on:
      gateway:
        condition: service_healthy
    environment:
      OPENAI_API_BASE_URL: http://gateway:8080/v1
      OPENAI_API_KEY: ${FITT_BEARER_TOKEN}
      ENABLE_SIGNUP: "false"
      WEBUI_AUTH: "true"
    volumes:
      - open-webui-data:/app/backend/data
    ports:
      - "3000:3000"
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"

volumes:
  open-webui-data:
```

### Environment variables (`.env` at repo root, gitignored)

```
# Path on the host for FITT's config, secrets, sessions, and logs.
# On QNAP typically: /share/FITT
# On Linux:          /srv/fitt  (or similar)
# On macOS:          /Users/you/.fitt
FITT_HOME=/share/FITT

# User/group that owns ${FITT_HOME} on the host. Matches the
# UID/GID the containers run as. QNAP admins default to 1000:1000.
PUID=1000
PGID=1000

# Bearer token Open WebUI presents to the gateway (same one clients
# use; read from ~/.fitt/secrets.yaml manually or scripted).
FITT_BEARER_TOKEN=REPLACE_WITH_TOKEN_FROM_SECRETS_YAML
```

Shipped as `.env.example`; added to `.gitignore` so real values
never land in git.

## Dev overlay (U4)

`docker-compose.override.yml` is auto-applied by compose when
present. We ship a `.example` so users opt in explicitly.

```yaml
# docker-compose.override.yml.example  -- copy to docker-compose.override.yml
services:
  gateway:
    # Bind-mount source so code edits are picked up without rebuild.
    volumes:
      - ./gateway/src:/app/src:ro
    # Run under uvicorn --reload
    command: ["uvicorn", "gateway.app:create_app", "--factory",
              "--host", "0.0.0.0", "--port", "8080", "--reload",
              "--reload-dir", "/app/src"]

  telegram-bot:
    volumes:
      - ./telegram-bot/src:/app/src:ro
    # No --reload equivalent baked in; restart the container on change:
    #   docker compose restart telegram-bot
```

VS Code Remote-SSH'd into the NAS, saving a gateway file triggers
uvicorn reload in ~1 second. Telegram bot changes still need a
manual restart, which is a rare enough path that it's not worth
adding watcher deps.

Caveat: `create_app` factory does not exist today. `gateway/__main__`
wires `app = FastAPI(...)` and runs uvicorn directly. Dev overlay
will need either a `create_app()` factory (cleanest) or a compose
command that invokes the module differently. Task item listed in
`tasks.md`.

## Interaction with existing configuration

Zero schema changes. Containers read the same `config.yaml` /
`secrets.yaml` that the Windows hub reads. Two small environment
adjustments:

- `FITT_HOME=/fitt` in the container overrides the
  `Path.home() / .fitt` default. Already supported by
  `gateway/src/gateway/config.py`.
- `FITT_CONFIG_PATH` and `FITT_SECRETS_PATH` are set explicitly in
  compose so the container doesn't get confused if `/fitt` ever
  has an unrelated subfolder structure.

### Secrets permissions (R1)

The compose file does not do anything special; the gateway's
existing POSIX mode-check applies inside the container. On the
host:

```bash
chmod 0600 ${FITT_HOME}/secrets.yaml
```

The quickstart walks through this. A failed permission check
produces the same clear error message today and Phase 3.5 doesn't
touch that code path.

## Tests

### Unit tests

No existing tests should change or regress. `uv run pytest` in
each package remains the source of truth for gateway/bot
behaviour.

### Smoke test

A new `scripts/smoke-compose.sh` (bash, POSIX-only) brings up the
stack with a minimal config, waits for `/health`, hits
`/v1/models`, brings it down. Intended to be run manually on the
NAS after a fresh clone. Not wired into CI until we have a CI
environment with Docker.

```bash
#!/usr/bin/env bash
set -euo pipefail
export FITT_HOME=$(mktemp -d)
cp configs/config.example.yaml "$FITT_HOME/config.yaml"
cp configs/secrets.example.yaml "$FITT_HOME/secrets.yaml"
chmod 0600 "$FITT_HOME/secrets.yaml"
# ... minimal edits ...
docker compose up -d gateway
for i in {1..30}; do
  if curl -sf http://localhost:8080/health >/dev/null; then break; fi
  sleep 2
done
curl -sf http://localhost:8080/v1/models
docker compose down
rm -rf "$FITT_HOME"
```

Matches the existing `scripts/` convention; PowerShell users aren't
expected to run smoke tests on Windows.

### Integration test (deferred)

Spinning up the full compose stack in a pytest fixture is tempting
but brittle (Docker state, port collisions). Defer until we have a
CI runner. Add a note in `gateway/README.md` pointing at the smoke
script.

## Quickstart changes

Restructure Part A into two lettered sub-paths:

- **Part A.1 — Windows hub** (current content, unchanged).
- **Part A.2 — Docker hub** (new, the QNAP path).

Part B (Satellites) and Part C (Clients) are unchanged regardless
of which A the user chose. The doc opens with a small decision box:

> Pick one:
> - **A.1** if your hub is Windows and you want the NSSM service path.
> - **A.2** if your hub is Linux, macOS, or a QNAP/Synology NAS, and
>   you prefer Docker.

Part A.2 is shorter than A.1 because Docker absorbs most of the
install complexity. Rough outline:

1. Install Docker / Container Station (link out).
2. Install Tailscale on the host.
3. Create OpenRouter account (same as A.1 step 2).
4. Clone repo, create `.env`, fill in `FITT_HOME` and
   `FITT_BEARER_TOKEN`.
5. Copy `configs/*.example.yaml` to `$FITT_HOME`, fill in.
6. `chmod 0600 $FITT_HOME/secrets.yaml`.
7. `docker compose up -d`.
8. Verify `/health` on `:8080` and Open WebUI on `:3000`.

A separate appendix "Developing on the NAS" covers the VS Code
Remote-SSH + override.yml workflow. Intended for maintainers, not
first-time installers.

## Rollout and migration

### Order of ops

1. Land the Dockerfiles and compose changes in one commit.
2. Land the quickstart restructure in a second commit.
3. Leave the Windows path completely untouched. Do not retire
   `install-service.ps1`.
4. Delete `install-open-webui.ps1` and its uninstall sibling. The
   compose file subsumes them. Note this in the commit message so
   people searching git history find the replacement.

### Migration for the author's hub

Documented explicitly in quickstart Part A.2 appendix, "Migrating
from a Windows hub":

1. Stop the three services on Windows (`Stop-Service FITTGateway`,
   etc.).
2. Copy `%USERPROFILE%\.fitt\` to `\\nas\share\FITT\` (or equivalent).
3. SSH to the NAS, `chmod 0600 /share/FITT/secrets.yaml`.
4. Clone the repo on the NAS, copy `.env.example` to `.env`, edit.
5. `docker compose up -d`.
6. Update Continue's `apiBase` on the laptop to the NAS's Tailscale
   IP.
7. Optionally uninstall Windows services on the old host with the
   existing `uninstall-*.ps1` scripts.

Memory (identity + sessions) is plain markdown. Costs log is plain
text. No migration tool needed.

## Open design decisions for review

1. **Open WebUI image tag.** Pin to a dated tag (e.g.
   `v0.3.35`) or float on `:main`? Pinning is safer; main is what
   Phase 3 shipped. Propose: pin to a recent stable tag, bump
   deliberately in a dedicated PR.
2. **`create_app` factory refactor.** The dev overlay wants this.
   It's a tiny refactor in `gateway/__main__` (extract FastAPI
   wiring into `gateway.app.create_app()`, keep `__main__` as the
   entrypoint). I'd include it in this phase since the dev overlay
   is a goal; otherwise the overlay is harder to keep clean.
3. **Registry publication.** Skipping for v0; images are built on
   the target host. If we later publish to GHCR, we'll add a
   workflow. Name the images `ghcr.io/qam4/fitt-gateway` and
   `ghcr.io/qam4/fitt-telegram-bot` so the future path is obvious.
4. **Compose project name.** Docker uses the directory name
   (`home-ai-cluster`) by default. Hardcoding `name: fitt` at the
   top of the compose file is cleaner for `docker compose logs`
   readability. Propose: add it.
