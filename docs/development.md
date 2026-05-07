# FITT — Development Workflow

Notes for the person working on FITT, not installing it. If you're
here to run FITT, [`docs/quickstart.md`](./quickstart.md) is the
door.

## Repo layout

- `gateway/` — FastAPI daemon. Where 90% of changes land.
- `telegram-bot/` — PTB bot that talks to the gateway via HTTP.
- `configs/config.example.yaml`, `configs/secrets.example.yaml` —
  schema docs; your real files live in `$FITT_HOME/` on the host.
- `.kiro/specs/phase<N>-<name>/` — per-phase spec
  (`requirements.md`, `design.md`, `tasks.md`). When a phase is
  active, this is the source of truth for what's being built.
- `FITT_ROADMAP.md` — the outer plan + inline draft specs for
  phases that haven't started yet.

## Typical development loop

Edit source → run tests locally → commit → git pull on the NAS →
rebuild → restart.

### On the dev machine (laptop)

From `gateway/` or `telegram-bot/`:

```
uv run pytest -q                 # everything
uv run pytest tests/test_x.py    # one file
uv run mypy src                  # types
uv run ruff format src tests     # format in place
uv run ruff check src tests      # lint
```

Commit checklist is codified in `.kiro/steering/commit-checklist.md`
— the short form:

1. `uv run pytest -q` passes
2. `uv run mypy src` clean
3. `uv run ruff format src tests` applied
4. `uv run ruff check src tests` clean
5. Spec `tasks.md` updated (`[x]` the completed tasks)

### On the NAS (hub)

```
cd /path/to/home-ai-cluster
git pull
docker compose build                 # rebuild gateway + bot
docker compose up -d                 # recreate containers
```

Shortcut if you always do both:

```
docker compose up -d --build
```

Target a single service when you only touched one:

```
docker compose up -d --build gateway
```

See **"When do I rebuild vs restart vs do nothing?"** below for
the less-obvious cases.

## When do I rebuild vs restart vs do nothing?

| What you changed | What to do |
|---|---|
| Python source under `gateway/src/` or `telegram-bot/src/` | `compose build <service>` + `compose up -d <service>`, or `compose up -d --build <service>` |
| `gateway/Dockerfile` or `telegram-bot/Dockerfile` | `compose build <service>` + `compose up -d <service>` |
| `docker-compose.yml` | `compose up -d` (compose re-creates containers whose spec changed) |
| `.env` | `compose up -d` (env is baked in at container-create time, so a restart alone won't pick up changes) |
| `config.yaml` in `$FITT_HOME` | `compose restart gateway` (config is read once at startup and cached on `app.state.config`) |
| `secrets.yaml` in `$FITT_HOME` | `compose restart gateway` (same as config.yaml) |
| `identity/*.md` in `$FITT_HOME` | **Nothing.** MemoryStore re-reads identity files on every request. |
| `projects.yaml` in `$FITT_HOME` | **Nothing.** ProjectRegistry re-reads on every `.get()`. |
| `cron.json` in `$FITT_HOME` | **Nothing.** CronService watches the file's mtime and hot-reloads on the next scheduler tick (~30s). |
| `events.jsonl`, `audit.jsonl`, `capability_gaps.log` | Not configuration — appending data files. Don't touch; the gateway owns them. |
| `configs/*.example.yaml` | **Nothing automatic.** These are schema docs; your real files under `$FITT_HOME` are untouched. Diff the new example against yours manually if you want to adopt new knobs. |
| `docs/*.md`, `README.md`, `FITT_ROADMAP.md`, specs | **Nothing.** Informational only. |

### When to use `compose build --no-cache`

Almost never. Docker's layer caching is correct for the common
cases — if you changed `src/gateway/foo.py`, the `COPY src/` layer
and everything below invalidates, the earlier `uv sync` layers
stay cached.

Reach for `--no-cache` only when you suspect a cached layer is
lying to you:

- You reverted a change but the image still behaves as if it has
  the old one.
- `pyproject.toml` / `uv.lock` changed but a previous failed
  build left broken artifacts in the cache.
- Docker cache corruption (rare; usually manifests as weird
  `FileNotFoundError`s during `uv sync`).

For ordinary changes, `compose build` is what you want. A typical
code-only rebuild takes 5–20 seconds; `--no-cache` turns that into
1–3 minutes, so don't reach for it reflexively.

### When to use `compose pull`

For this repo today: effectively never. The gateway and bot have
`build:` blocks in `docker-compose.yml`, so `pull` does nothing
for them. The only `image:`-without-build service is
`open-webui`, pinned to a specific tag. `compose pull open-webui`
would update that tag if it moved — but we pin deliberately, so
you'd bump the tag in a PR before running `pull`.

If we ever publish prebuilt gateway/bot images (probably via
GHCR), the `build:` blocks get swapped for `image:` blocks and
`compose pull` becomes the normal update path. Not today.

## Phase / spec discipline

When a new phase starts:

1. Read the inline draft in `FITT_ROADMAP.md`.
2. Promote it to `.kiro/specs/phase<N>-<name>/` with three files:
   `requirements.md`, `design.md`, `tasks.md`. Match the shape
   of `phase1-gateway/`.
3. Keep tasks in `tasks.md` checkboxed with stable IDs (`1a`,
   `1b`, `2a`, …) so tests and commits can reference them.
4. `[x]` tasks as they land; don't delete completed ones.

Steering files under `.kiro/steering/` govern agent behaviour on
the project (commit checklist, conventions, project overview).
Read them before making structural changes.

## Docs inventory

- [`README.md`](../README.md) — landing page.
- [`docs/quickstart.md`](./quickstart.md) — installer's view of
  FITT. One page, ~30 min.
- [`docs/faq.md`](./faq.md) — recurring user questions.
- [`docs/development.md`](./development.md) — this file.
- [`gateway/README.md`](../gateway/README.md) — config reference,
  HTTP API, CLI, troubleshooting.
- [`FITT_ROADMAP.md`](../FITT_ROADMAP.md) — guiding principles,
  phase plan, inline drafts for upcoming phases.
- [`FITT_PRD.md`](../FITT_PRD.md) — original product
  requirements. Historical reference; roadmap is current.
