---
inclusion: auto
description: Build, test, and verification commands before committing FITT changes
---

# Build & Test Commands

Run from `gateway/`:

1. Test: `uv run pytest -q`
2. Type check: `uv run mypy src`
3. Format: `uv run ruff format src tests`
4. Lint: `uv run ruff check src tests --fix`

For the `fitt` CLI end-to-end, `uv run fitt config check` against a
test config.

## PowerShell scripts

After editing `scripts/*.ps1`:

- Verify no Unicode dashes:
  `Select-String -Path scripts/*.ps1 -Pattern "[\u2010-\u2015]"`
- Verify UTF-8 BOM is present on each `.ps1` (PS 5.1
  compatibility).
- Verify the script parses:
  `[System.Management.Automation.Language.Parser]::ParseFile(<path>, [ref]$null, [ref]$errors)`

## Docs

If editing `docs/*.md` or `README.md`, check for:

- Links to other docs still resolve (after we consolidated, the
  only surviving docs are `docs/quickstart.md` and
  `gateway/README.md`).
- No references to deleted files (`docs/prerequisites.md`,
  `docs/accounts-setup.md`).

## Spec

If editing `.kiro/specs/phase<N>-<name>/tasks.md`:

- Mark `[x]` for completed tasks, leave `[ ]` for at-home-only
  runtime tasks (service install, reboot, port scan, IDE wiring).
- Don't delete completed tasks; the file is a phase log.

## Project rules (from roadmap)

- Models are configuration, not architecture - new models go in
  `config.yaml`, not in code.
- Clients use aliases only (`fitt-default` etc.), never concrete
  model IDs.
- No personal values in the repo.
- Use uv for Python management, not pip/venv directly.
- Use ASCII hyphens only in `.ps1`, `.yaml`, and `.toml` files.
