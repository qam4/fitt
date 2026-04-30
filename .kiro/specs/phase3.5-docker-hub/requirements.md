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
5. **Windows service path is legacy, not maintained.** The
   existing `install-service.ps1` and `install-telegram-bot.ps1`
   remain in the tree but are not exercised or documented in the
   new quickstart. They either keep working by accident or they
   don't; we don't spend effort keeping them green.
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

### U1 — First install on a QNAP via Container Station GUI

> As a QNAP user comfortable with Container Station for apps like
> Jellyfin, I want to install FITT without memorizing SSH commands.
> I'll happily paste a compose file into the Container Station
> "Applications" panel, but I don't want to click through the
> "Create Container" wizard three times.

Acceptance:
- Container Station's "Applications" -> "Create" accepts our
  `docker-compose.yml` as-is, with no hand-editing.
- All three services come up from that one form submission.
- `curl http://<nas-tailscale-ip>:8080/health` returns 200 within
  45 seconds of the Create click.
- The dashboard groups the three containers under one application
  name (`fitt`) so start/stop/logs are managed together.

### U1b — First install via SSH

> As a user who prefers the terminal, or who wants the faster
> iteration loop, I want `docker compose up -d` at the repo root
> to bring up the whole hub.

Acceptance:
- A fresh SSH session with Docker installed: `git clone`,
  `cp .env.example .env`, edit, `docker compose up -d` produces
  the same stack as U1.
- `curl http://<host-ip>:8080/health` returns 200 within 45s.
- Open WebUI reachable at `:3000`.

### U1c — depends_on health condition compatibility

> As an operator, I do not want the install to fail on QTS /
> Container Station versions that don't fully honor
> `depends_on: condition: service_healthy`.

Acceptance:
- If `condition: service_healthy` is rejected or ignored, the bot
  and Open WebUI still come up and eventually connect to the
  gateway once it's ready (they retry; the gateway's own
  healthcheck keeps Docker's state accurate).
- Documented in the quickstart: if the Container Station GUI
  parse errors on a `condition:` key, drop it and proceed.

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

### U3 — Single install path going forward

> As the author and any future user, I want one install path (the
> Docker one) so I don't pay the cost of maintaining two ways to
> do the same thing.

Acceptance:
- Quickstart documents only the Docker hub install. No Windows
  service path in the docs.
- `install-service.ps1` and `install-telegram-bot.ps1` remain in
  `scripts/` as legacy files. No code or config changes in this
  phase are required to keep them working, and if they break as
  a side effect of other work, they stay broken until someone
  files it as a bug.
- The Docker path works on Windows + Docker Desktop, Linux, and
  macOS, so no OS loses access to FITT.

### U4 — Iteration loop on the laptop

> As the maintainer, I want to iterate on the hub stack locally on
> my laptop using the same images I deploy, so the "works on my
> machine" case stays honest.

Acceptance:
- A `docker-compose.override.yml.example` exists that:
  - Bind-mounts `./gateway/src` into the container.
  - Runs the gateway under `uvicorn --reload` so source edits are
    picked up live.
- Copied to `docker-compose.override.yml` locally, running
  `docker compose up -d` on the laptop brings up the same three
  services as on the NAS, pointing at a local `$FITT_HOME`.
- Dependency changes (adding a package to `pyproject.toml`) require
  a rebuild; source-only changes do not.
- The same override file works unchanged on the NAS for the rare
  case when a bug only reproduces there.

Note: the primary dev loop is the laptop. The NAS is a
**deployment target**, not a workspace. Remote VS Code on the NAS
is a diagnosis tool for NAS-specific issues, documented in the
appendix but not required for feature work.

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
- Quickstart rewritten around the single Docker hub path. Parts B
  (Satellites) and C (Clients) carry over unchanged.
- Short "dev on the laptop" notes in the gateway README
- Retire `install-open-webui.ps1` (compose handles Open WebUI now)

Out of scope (future phases):
- Image publishing to a registry (manual `docker compose build` on
  the host is fine for v0)
- Admin web UI
- Multi-host deployments
- Backup automation (a cron snapshot of `~/.fitt/` is enough for v0)
- **Active maintenance of `install-service.ps1` /
  `install-telegram-bot.ps1`.** They stay in the tree for anyone
  who finds them useful, but they are not covered by the quickstart,
  not in the acceptance criteria, and not kept green as the rest of
  the code moves. Treat as archived.

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

Decision: the install uses Container Station's "Applications" ->
"Create from compose" flow, not the per-container "Create
Container" wizard. That keeps the compose file as the source of
truth. The per-container GUI is read-only for us; modifications
there are documented as "will be lost on next `docker compose up`."

### R5b — depends_on: condition support

Some QTS / Container Station versions accept
`depends_on: condition: service_healthy`, some reject it, some
parse it but ignore the condition.

Decision: keep `condition: service_healthy` in the canonical
compose file because it's correct and works on current Docker
Engine. Document the fallback in the quickstart: if Container
Station errors on parse, replace the `depends_on:` blocks with
the simple list form (`depends_on: [gateway]`). The bot and Open
WebUI retry their connections anyway, so the net effect is a few
extra log lines at startup and no behaviour difference after
~10 seconds.

### R6 — Image arch

Target must run on x86_64 (TS-253Be), and ARM64 is nice to have
(for M-series Macs and ARM SBCs people might use as hubs).

Decision: use `python:3.11-slim` as the base image, which is
multi-arch. Build-time `docker buildx` usage is optional for v0;
single-arch builds on the target host work fine.

## Success criteria

Phase 3.5 is done when:

1. A fresh user with a Linux, macOS, QNAP + Container Station, or
   Windows + Docker Desktop box can clone the repo, fill in
   `config.yaml` and `secrets.yaml` under a chosen directory, run
   `docker compose up -d`, and hit `/health` from their Tailscale
   network within one minute.
2. All existing gateway and telegram-bot tests still pass
   (`uv run pytest` in each package).
3. A new integration test or smoke script brings up the compose
   stack, hits `/health` and `/v1/models` through the gateway
   container, and tears down cleanly. (Skipped in CI if Docker
   isn't available.)
4. The quickstart is a single linear path (Hub via Docker -> Satellites
   -> Clients). No Windows service path in the docs.
5. The author's QNAP hub has been running for at least 3 days
   without the author having to SSH in to fix anything.
