# Phase 3.5 — Docker-first Hub: Tasks

Implementation order picked to keep the tree green at every commit.
Each top-level task is a reviewable commit.

## 1. Gateway image

- [ ] 1a. Add `gateway/.dockerignore` excluding `.venv`, `__pycache__`,
       `.pytest_cache`, `.mypy_cache`, `tests/`, `dist/`, `*.egg-info`.
- [ ] 1b. Extract a `create_app()` factory in
       `gateway/src/gateway/app.py` (already exists) and make
       `gateway/__main__.py` a thin entrypoint that calls it plus
       `uvicorn.run(...)`. No behaviour change.
- [ ] 1c. Write `gateway/Dockerfile` (multi-stage, python:3.11-slim,
       uv in builder, runtime has no uv).
- [ ] 1d. Verify: `docker build -t fitt-gateway:local ./gateway`
       succeeds on Linux/macOS. Image size < 200 MB.
- [ ] 1e. Verify: `docker run --rm -e FITT_HOME=/tmp/nope
       fitt-gateway:local python -c "import gateway; print('ok')"`
       exits 0.

## 2. Telegram bot image

- [ ] 2a. Add `telegram-bot/.dockerignore`.
- [ ] 2b. Write `telegram-bot/Dockerfile` mirroring the gateway's
       shape.
- [ ] 2c. Verify: `docker build -t fitt-telegram-bot:local
       ./telegram-bot` succeeds.

## 3. Compose topology

- [ ] 3a. Rewrite `docker-compose.yml` at repo root to define
       `gateway`, `telegram-bot`, and `open-webui` services per
       design.md. Pin Open WebUI to a specific stable tag.
- [ ] 3b. Add `name: fitt` at the top of the compose file for clean
       log prefixes.
- [ ] 3c. Add `logging.options.max-size=10m, max-file=5` to each
       service.
- [ ] 3d. Add `.env.example` at repo root documenting `FITT_HOME`,
       `PUID`, `PGID`, `FITT_BEARER_TOKEN`.
- [ ] 3e. Add `.env` to `.gitignore` (verify it already is).
- [ ] 3f. Remove the old Open-WebUI-only content from whatever
       `docker-compose.yml` or `docker-compose.open-webui.yml` holds
       it today. Confirm by grep that nothing else in the repo
       refers to those retired script paths.

## 4. Retire the Open WebUI install scripts

- [ ] 4a. `git rm scripts/install-open-webui.ps1
       scripts/uninstall-open-webui.ps1`.
- [ ] 4b. Search the repo for any references to those script names;
       update or remove.
- [ ] 4c. Quickstart Part A.1 (Windows) updated: point users at the
       compose path even on Windows for Open WebUI. (Phase 3 already
       used compose for this, so the net change is removing the
       `.ps1` wrapper.)

## 5. Dev overlay

- [ ] 5a. Write `docker-compose.override.yml.example` per design.md,
       bind-mounting gateway source and running under
       `uvicorn --reload`.
- [ ] 5b. Add `docker-compose.override.yml` to `.gitignore`.
- [ ] 5c. Document the "dev on the NAS" workflow (VS Code
       Remote-SSH + override file + `docker compose logs -f gateway`)
       in `gateway/README.md`. Short, one section.

## 6. Smoke test

- [ ] 6a. Write `scripts/smoke-compose.sh` per design.md.
- [ ] 6b. Test it locally on Linux or macOS: fresh clone, fill in
       placeholder values, run the script, confirm it passes.
- [ ] 6c. Mention in `gateway/README.md` troubleshooting section as
       the first thing to run when the stack misbehaves.

## 7. Quickstart restructure

- [ ] 7a. Split Part A into A.1 (Windows) and A.2 (Docker). Keep
       the existing A content as A.1 verbatim.
- [ ] 7b. Write A.2 following the outline in design.md: 8 steps,
       ending at a working `/health` response.
- [ ] 7c. Add the one-paragraph chooser at the top of Part A so
       readers pick the right path immediately.
- [ ] 7d. Add a small "Migrating from a Windows hub to a Docker
       hub" section as an appendix to A.2.
- [ ] 7e. Parts B (Satellites) and C (Clients) are unchanged;
       verify cross-links still land.

## 8. QNAP pilot

(Manual, documented in spec review.)

- [ ] 8a. Author: on the TS-253Be, create `/share/FITT/`, copy
       existing `~/.fitt/` content, chmod secrets.
- [ ] 8b. Clone the repo on the NAS.
- [ ] 8c. Fill in `.env`.
- [ ] 8d. `docker compose up -d`.
- [ ] 8e. Verify `/health`, `/v1/models`, Telegram `/start`,
       Open WebUI signup.
- [ ] 8f. Switch Continue on the laptop to the NAS's Tailscale IP.
- [ ] 8g. Live with it for 3 days. File any friction as follow-up
       tasks.

## 9. Docs cleanup

- [ ] 9a. Gateway README `config.yaml structure` section: mention
       that `FITT_HOME` is set to `/fitt` in the Docker path.
- [ ] 9b. Gateway README troubleshooting: add "Docker: container
       exits immediately" entry that points at PUID/PGID and
       secrets file permissions.
- [ ] 9c. `FITT_ROADMAP.md`: note Phase 3.5 as completed in the
       phase history once the pilot passes.

## Definition of done

- Fresh Linux, macOS, or QNAP host can `docker compose up -d` and
  hit `/health` within 60 seconds of start.
- All existing tests pass (`uv run pytest` in gateway/ and
  telegram-bot/).
- Windows hub path (A.1) still works end-to-end.
- Author has run for 3 days on the NAS without intervention.
- Two commits: one for the Docker work, one for the quickstart
  restructure. Cleanly reviewable.
