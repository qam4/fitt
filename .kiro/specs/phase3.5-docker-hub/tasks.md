# Phase 3.5 — Docker-first Hub: Tasks

Implementation order picked to keep the tree green at every
commit. Each top-level task is a reviewable commit.

Status legend: `[x]` done and on `main`, `[ ]` not yet.

## 1. Gateway image

- [x] 1a. Add `gateway/.dockerignore`.
- [x] 1b. Add a zero-arg `create_app_from_env()` factory in
       `gateway/src/gateway/app.py` for `uvicorn --factory` /
       `--reload`. (Existing `create_app(config)` kept for tests.)
- [x] 1c. Write `gateway/Dockerfile` (multi-stage,
       python:3.11-slim, uv in builder, runtime has no uv,
       non-root `fitt` user at uid/gid 1000, healthcheck on
       `/health`).
- [ ] 1d. Verify on a real Docker host: `docker build -t
       fitt-gateway:local ./gateway` succeeds, image size
       < 200 MB. (Sandbox has no daemon; deferred to pilot.)

## 2. Telegram bot image

- [x] 2a. Add `telegram-bot/.dockerignore`.
- [x] 2b. Write `telegram-bot/Dockerfile`. Build context must be
       the repo root (not `./telegram-bot`) so `tool.uv.sources`
       can resolve `../gateway`.
- [ ] 2c. Verify on a real Docker host: `docker build -t
       fitt-telegram-bot:local -f telegram-bot/Dockerfile .`
       succeeds. (Deferred to pilot.)

## 3. Compose topology

- [x] 3a. Rewrite `docker-compose.yml` at repo root with gateway,
       telegram-bot, and open-webui services per design.md.
       Open WebUI pinned to `v0.3.35`.
- [x] 3b. Added `name: fitt` at the top for clean log prefixes.
- [x] 3c. Added `logging.options.max-size=10m, max-file=5` to
       each service.
- [x] 3d. Added `.env.example` at repo root with `FITT_HOME`,
       `PUID`, `PGID`, `TZ`, `FITT_BEARER_TOKEN`.
- [x] 3e. `.env` confirmed gitignored.
- [x] 3f. Both bind mounts point under `$FITT_HOME`: one for the
       gateway/bot pair, one for Open WebUI's own state.

## 4. Retire the Open WebUI install scripts

- [x] 4a. Deleted `scripts/install-open-webui.ps1` and
       `scripts/uninstall-open-webui.ps1`.
- [x] 4b. Grep confirmed no remaining references inside the repo
       other than historical spec/roadmap mentions.

## 5. Dev overlay

- [x] 5a. Wrote `docker-compose.override.yml.example` that
       bind-mounts gateway/src (and telegram-bot/src), overrides
       the gateway ENTRYPOINT with `uvicorn --factory --reload`
       pointed at `create_app_from_env`.
- [x] 5b. Added `docker-compose.override.yml` to `.gitignore`.
- [x] 5c. Added a "Running the whole hub in Docker (on your
       laptop)" section to `gateway/README.md` explaining the
       hot-reload dev loop.

## 6. Smoke test

- [x] 6a. Wrote `scripts/smoke-compose.sh` (POSIX bash, marked
       +x in git's index). Builds gateway, brings it up with a
       mktemp FITT_HOME, polls /health, checks /v1/models, tears
       down.
- [ ] 6b. Verify on a real Docker host: script passes on Linux
       or macOS. (Deferred to pilot.)
- [x] 6c. Pointed at the script from `gateway/README.md`'s
       troubleshooting section as the first thing to run when a
       compose install misbehaves.

## 7. Quickstart rewrite

Rewritten around the Docker hub as the single supported path
(not a two-flavor A.1/A.2 split as originally outlined - the
Windows NSSM path became unmaintained legacy per discussion,
so no point in documenting it alongside the Docker one).

- [x] 7a. Part A rewrites Steps 3-7 around install Docker, clone
       repo, populate `$FITT_HOME`, write `.env`,
       `docker compose up -d`, verify.
- [x] 7b. Step 6 offers two equivalent flavors (6.A Container
       Station GUI via "Applications", 6.B SSH + compose).
- [x] 7c. Parts B (Satellites) and C (Clients) updated: restart
       gateway is now `docker compose restart gateway`;
       Telegram bot and Open WebUI in Part C are just "configure
       and verify" since the containers were already started.
- [x] 7d. Platform-neutral Tailscale and editor instructions
       (Step 1, Step 11) so Linux/macOS/QNAP users aren't
       reading PowerShell.
- [x] 7e. Resilience checks and common slip-ups rewritten for
       Docker (compose restart policy, uid/gid mismatch,
       depends_on condition on older QTS).
- [x] 7f. Added "Updating the hub" section with the
       `git pull && docker compose build && docker compose up -d`
       three-liner.

## 8. QNAP pilot (manual, author only)

- [ ] 8a. On the TS-253Be, create `/share/Public/fitt/` matching
       the existing Jellyfin/Plex app-data convention. Copy
       existing `~/.fitt/` content over. `chmod 0600` secrets.
- [ ] 8b. Clone the repo on the NAS.
- [ ] 8c. Fill in `.env` (`FITT_HOME`, `PUID`, `PGID`, `TZ`,
       `FITT_BEARER_TOKEN`).
- [ ] 8d. `docker compose up -d` (or Container Station
       Applications -> Create).
- [ ] 8e. Verify `/health`, `/v1/models`, Telegram `/start`,
       Open WebUI signup.
- [ ] 8f. Switch Continue on the laptop to the NAS's Tailscale
       IP.
- [ ] 8g. Live with it for 3 days. File any friction as
       follow-up tasks.

## 9. Docs cleanup

- [x] 9a. Gateway README environment-variables section: notes
       that `FITT_HOME=/fitt` in the container and is bind-mounted
       from the host; `FITT_CONFIG_PATH` / `FITT_SECRETS_PATH`
       set explicitly in the compose file.
- [x] 9b. Gateway README troubleshooting: added "Docker:
       container exits immediately after `docker compose up`"
       entry covering secrets permissions, UID/GID mismatch, and
       config validation errors.
- [x] 9c. `FITT_ROADMAP.md`: Phase 3.5 marked "CODE LANDED" with
       a status paragraph. Will flip to "shipped" after the pilot
       passes.

## Follow-up (not part of this phase)

- [ ] After 2+ weeks on the Docker hub without the author wanting
      to fall back, decide whether to retire
      `install-service.ps1`, `install-telegram-bot.ps1`, and their
      uninstall counterparts. If retired, do so as a deliberate
      commit with context, not a drive-by cleanup.

## Definition of done

- Fresh Linux, macOS, or QNAP host can `docker compose up -d`
  and hit `/health` within 60 seconds of start. **(pending
  pilot)**
- All existing tests pass (`uv run pytest` in gateway/ and
  telegram-bot/). **(verified: 123 + 33 green)**
- Quickstart is a single linear path; no NSSM references in the
  docs. **(done)**
- Author has run for 3 days on the NAS without intervention.
  **(pending pilot)**
