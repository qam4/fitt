# Phase 3.5 — Docker-first Hub

## Background

Phases 1 through 3 shipped a Windows-only hub: a gateway, a Telegram
bot, and Open WebUI, each installed as a Windows service via NSSM or
a separate docker-compose for Open WebUI. This works on the author's
desktop but doesn't fit well elsewhere - especially on a NAS, which
is the more natural home for an always-on hub.

This phase makes the hub **portable and Docker-first** without
rewriting the gateway itself. The same Python packages we already
ship become Docker images, composed together in one
`docker-compose.yml`. The target deployment is a QNAP NAS running
Container Station, but the same compose file works on any Linux
host, on macOS via Docker Desktop, and on Windows via Docker Desktop.

Satellites are explicitly out of scope. Running Ollama natively on
the machine with the GPU is the right choice and shouldn't change.

## Why now

- The author's QNAP TS-253Be is a better fit for an always-on hub
  than the desktop (lower power, better uptime, persistent storage
  with snapshots).
- Shareable-by-construction means someone picking up this repo
  should not need Windows + NSSM + PowerShell to try it. Docker +
  compose is the lowest common denominator for "I want to run a
  server app on some box I own."
- Phase 3 already introduced Open WebUI via docker-compose, so the
  ecosystem is partially there. This phase finishes the job by
  moving the gateway and the Telegram bot onto the same compose
  file.

## Goals

1. **One command, whole hub.** `docker compose up -d` at the repo
   root brings up the gateway, the Telegram bot, and Open WebUI on
   any Docker host.
2. **Portable by default.** No host-specific paths, no Windows-only
   tools, no GPU assumptions. Runs on x86 and ARM.
3. **Same configuration surface.** The container reads
   `~/.fitt/config.yaml` and `~/.fitt/secrets.yaml` from a bind-
   mounted volume. Zero changes to the config file shape from
   Phase 1-3.
4. **Zero data migration.** A user switching a Windows hub to a
   Docker hub copies their `~/.fitt/` directory to the new host and
   starts the stack. Identity, sessions, memory, and logs are all
   markdown/JSON on disk and fully portable.
5. **Windows service path stays working.** The `install-service.ps1`
   / NSSM path stays supported for users who want it. New path is
   additive.
6. **Dev loop on the NAS.** A developer can edit the gateway on
   their laptop with VS Code Remote-SSH, have the running container
   pick up source changes via bind-mount + `--reload`, and commit
   from the same session.

## Non-goals

- **Satellites in Docker.** Ollama wants direct GPU access and is
  happiest running natively. Out of scope.
- **Kubernetes or Swarm.** Single-host docker-compose only. This is
  a home lab.
- **Multi-tenant auth inside the hub.** Still one Bearer token for
  the whole thing. Open WebUI owns its own user accounts already.
- **Custom images for Open WebUI.** Keep pulling the upstream
  `ghcr.io/open-webui/open-webui` image; we don't fork.
- **Admin web UI for editing config/secrets.** Separate future
  phase; this phase only containerizes what exists.
- **Automated image publishing to a registry.** v0 builds images
  locally on the target host. Registry publication (Docker Hub,
  GHCR) is a separate future concern.

## User stories

### U1 — First install on a QNAP

> As a home lab user with a QNAP NAS, I want to bring up the whole
> FITT hub with one `docker compose up -d`, reading my config from
> a bind-mounted share, so that my NAS is my hub.

Acceptance:
- `docker compose up -d` succeeds with just a `config.yaml` and
  `secrets.yaml` in the bind-mounted directory.
- `curl http://<nas-tailscale-ip>:8080/health` returns 200 within
  45 seconds of `up -d`.
- Open WebUI reachable at `:3000`, Telegram bot responds to
  `/start` in the app.

### U2 — Migrate from a Windows hub

> As someone running FITT on a Windows desktop today, I want to
> move to a Docker hub without losing my chat history, identity, or
> costs log.

Acceptance:
- Stop `FITTGateway` and `FITTTelegramBot` services on the old host.
- Copy `~/.fitt/` to the new host's bind-mount path.
- `docker compose up -d` on the new host.
- `fitt memory show user` on the new host returns the same content
  as before. Past session history files are all present under
  `sessions/<name>/history/`.
- Existing Continue IDE config only needs `apiBase` updated to the
  new host's Tailscale IP.

### U3 — Windows hub still works

> As an existing Windows-hub user, I do not want this phase to
> change my install or break my setup.

Acceptance:
- `install-service.ps1 -SetupVenv` on a clean Windows host still
  produces a working hub.
- Existing Telegram bot and Open WebUI install scripts still work.
- Quickstart Part A (Windows) is unchanged except for a cross-
  reference to Part A.2 (Docker).

### U4 — Developing on the NAS

> As the maintainer, I want a tight iteration loop when working on
> the gateway while it runs on my NAS, so I don't dread fixing
> bugs.

Acceptance:
- A `docker-compose.override.yml` (or equivalent) exists that:
  - Bind-mounts `./gateway/src` into the container.
  - Runs the gateway under `uvicorn --reload` (or equivalent) so
    source edits are picked up live.
- VS Code Remote-SSH'd into the NAS, an edit to a gateway module
  shows up in `docker compose logs -f gateway` within 2 seconds.
- Dependency changes (adding a package to `pyproject.toml`) require
  a rebuild; source-only changes do not.

### U5 — Updates

> As a hub operator, I want to pull new FITT code and restart only
> the services that changed.

Acceptance:
- `git pull && docker compose build gateway && docker compose up -d gateway`
  rebuilds and restarts only the gateway. Telegram bot and Open
  WebUI stay up throughout.
- The gateway re-reads `config.yaml` and `secrets.yaml` on
  container start. No image rebuild is needed for config-only
  changes; a `docker compose restart gateway` is sufficient.

## Scope boundaries

In scope:
- `gateway/Dockerfile`
- `telegram-bot/Dockerfile`
- Root `docker-compose.yml` with all three services
- A dev `docker-compose.override.yml.example` for the hot-reload
  workflow in U4
- Quickstart restructure: Part A.1 (Windows), Part A.2 (Docker),
  shared Part B (Satellites) and Part C (Clients)
- Short "dev on the NAS" notes in the gateway README
- Retire `install-open-webui.ps1` (compose handles Open WebUI now),
  keep `install-service.ps1` and `install-telegram-bot.ps1` for the
  Windows path

Out of scope (future phases):
- Image publishing to a registry (manual `docker compose build` on
  the host is fine for v0)
- Admin web UI
- Multi-host deployments
- Backup automation (a cron snapshot of `~/.fitt/` is enough for v0)

## Risks and open questions

### R1 — Secrets file permissions in a container

Today the gateway refuses to load `secrets.yaml` if it's
group/world-readable on POSIX. In a container the bind-mount
inherits the host's permissions, and QNAP shares often default to
"everyone" permissions.

Decision: document a `chmod 0600` step in the quickstart for the
Docker path. Do NOT weaken the gateway's permission check. Users
who land on a permission error get a clear log message pointing at
the fix. A future phase may use Docker's `secrets:` system for
tighter handling.

### R2 — uid/gid mismatch between host and container

The container's process may run as a different UID than the host's
file owner, causing write failures on session history files.

Decision: the Docker image's `ENTRYPOINT` honours `PUID` and `PGID`
environment variables (LinuxServer.io convention) and re-exec's as
that user before starting the gateway. Default `PUID=1000,
PGID=1000`, which is the QNAP admin default. Documented in
quickstart A.2.

### R3 — Telegram bot singleton

Only one bot program can long-poll a given Telegram token at a
time; a second instance gets 409 Conflict. If a user accidentally
leaves the Windows service running while starting the Docker
container, messages will be inconsistent.

Decision: the quickstart migration path tells the user to stop the
Windows service first. The bot container logs the 409 prominently
if it ever happens. No code-level lock - this is a one-line op
discipline.

### R4 — Log rotation

Docker's default JSON log driver grows unbounded.

Decision: set `logging.options.max-size=10m` and
`logging.options.max-file=5` on each service in the compose file.

### R5 — Container Station GUI vs compose drift

Container Station's GUI can modify containers in ways that diverge
from the compose file.

Decision: document "use `docker compose` via SSH, treat the GUI as
read-only" in the quickstart.

### R6 — Image arch

Target must run on x86_64 (TS-253Be), and ARM64 is nice to have
(for M-series Macs and ARM SBCs people might use as hubs).

Decision: use `python:3.11-slim` as the base image, which is
multi-arch. Build-time `docker buildx` usage is optional for v0;
single-arch builds on the target host work fine.

## Success criteria

Phase 3.5 is done when:

1. A fresh user with a Linux or macOS box (or QNAP with Container
   Station) can clone the repo, fill in `config.yaml` and
   `secrets.yaml` under a chosen directory, run
   `docker compose up -d`, and hit `/health` from their Tailscale
   network within one minute.
2. All existing gateway and telegram-bot tests still pass
   (`uv run pytest` in each package).
3. A new integration test or smoke script brings up the compose
   stack, hits `/health` and `/v1/models` through the gateway
   container, and tears down cleanly. (Skipped in CI if Docker
   isn't available.)
4. The quickstart renders cleanly with the Windows path and Docker
   path side by side and one link hand-off between them.
5. The author's QNAP hub has been running for at least 3 days
   without the author having to SSH in to fix anything.
