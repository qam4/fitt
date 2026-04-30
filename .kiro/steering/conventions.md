---
inclusion: always
description: Coding and tooling conventions for FITT
---

# Conventions

## Python tooling

- **uv** manages Python, virtualenv, and dependencies. `uv sync`,
  `uv run <cmd>`, `uv tool install --editable .`. Don't introduce
  `pip`, `venv`, or `poetry` directly.
- **Python 3.11+** (`requires-python = ">=3.11"` in pyproject).
- **Dependencies** go in `pyproject.toml` under `[project]
  dependencies` or `[project.optional-dependencies].dev`. Pinned
  versions in `uv.lock` (committed to git).

## Code style

- **Formatter**: `ruff format src tests`
- **Linter**: `ruff check --fix src tests`
- **Type checker**: `mypy` strict mode.
- **Test runner**: `pytest` (configured in `pyproject.toml`).
- **Property tests**: `hypothesis`, min 100 iterations, tagged in a
  comment referencing the design property number, e.g.
  `# Phase 1, Property 4: Cost calculation`.

## ASCII hyphens in files that machines parse

Use ASCII hyphens (`-`) only in PowerShell scripts, YAML files, and
TOML files. Avoid Unicode em-dashes (U+2014) and en-dashes
(U+2013) in these files - PowerShell 5.1 parses scripts with the
system ANSI codepage unless there's a UTF-8 BOM, and em-dashes
silently corrupt into invalid bytes.

Markdown is fine with em-dashes; GitHub and any markdown renderer
handles UTF-8 correctly.

## Shareable by construction

- No personal values in the repo. Tailscale IPs, tokens, API keys,
  hostnames, and local paths live in `~/.fitt/` on each user's
  machine. Only `.example` templates are in git.
- No hardcoded paths or names in code. Everything user-specific
  comes from `config.yaml` or `secrets.yaml`.
- `configs/*.example.yaml` documents the schema with placeholder
  values.
- `.gitignore` must exclude `secrets.yaml`, `*.db`, `logs/`,
  `.venv/`.

## Aliases, not model IDs

Clients name logical aliases (`fitt-default`, `fitt-smart`,
`fitt-fast`) in their `model` field. The gateway rejects concrete
model IDs (`qwen2.5-coder:14b`, `anthropic/claude-sonnet-4.5`) with
HTTP 400.

When adding a new model, add a `models:` entry in `config.yaml` and
bind an alias to it. No code changes.

## Kiro spec discipline

- Promote a phase's inline draft from `FITT_ROADMAP.md` into a
  proper three-file spec under `.kiro/specs/phase<N>-<name>/`
  BEFORE implementing.
- `requirements.md`, `design.md`, `tasks.md` - match the shape of
  the existing `phase1-gateway/` spec.
- Keep acceptance criteria numbered (1.1.1, 2.3, etc.) so tests can
  reference them.
- Mark tasks `[x]` as they complete. Don't delete completed tasks.

## Commit messages

- First line: 50-72 chars, imperative ("Add X", "Fix Y",
  "Migrate Z").
- Blank line.
- Body: what and why, not how. Use present tense. Bullet lists
  fine. Reference phase tasks by number when relevant.
- For runtime changes, note what was manually verified (e.g.
  "Verified: uv sync creates .venv, pytest passes, /health 200").

## Windows service scripts

- PowerShell scripts in `scripts/` that will be parsed by
  PowerShell 5.1 must be saved with a UTF-8 BOM to avoid ANSI
  codepage misparses.
- `install-service.ps1` and `uninstall-service.ps1` must be
  idempotent.
- Never rely on user PATH resolution for which Python to use - look
  up `<repo>\gateway\.venv\Scripts\python.exe` by filesystem path.
