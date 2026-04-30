# Tasks: FITT Phase 3 — Telegram + Browser Interface

## Phase 3a — Telegram bot scaffold

- [ ] 1. Create `telegram-bot/` with its own `pyproject.toml`,
  `uv.lock`, and `src/fitt_telegram_bot/` layout.
- [ ] 2. Dependencies: `python-telegram-bot[rate-limiter]>=21`,
  `httpx`, `pydantic`, `pyyaml`, `structlog`.
- [ ] 3. Dev dependencies: `pytest`, `pytest-asyncio`, `respx`,
  `ruff`, `mypy`.
- [ ] 4. `[project.scripts]` entry `fitt-telegram-bot` ->
  `fitt_telegram_bot.__main__:main`.
- [ ] 5. `src/fitt_telegram_bot/__init__.py` with `__version__`.

## Phase 3b — Config loading

- [ ] 6. `config.py`: reads `~/.fitt/config.yaml` and
  `~/.fitt/secrets.yaml` using the gateway's existing loaders
  (import from `gateway.config`). Adds its own typed bundle:
  `TelegramBotConfig(bot_token, allowlist, gateway_url,
  bearer_token)`.
- [ ] 7. Refuses to start if `bot_token` is missing.
- [ ] 8. Refuses to start if allowlist is absent (empty list is
  allowed and means "no one").
- [ ] 9. Tests for config parsing (valid, missing token, missing
  allowlist).

## Phase 3c — Preferences store

- [ ] 10. `prefs.py`: `ChatPrefs` dataclass + `PrefsStore` class
  with `get`, `set_alias`, `set_session`, atomic-write pattern.
- [ ] 11. Missing file -> defaults. Corrupted JSON -> log warning
  + defaults.
- [ ] 12. Unit tests (see Testing Strategy).

## Phase 3d — Gateway client

- [ ] 13. `gateway_client.py`: `GatewayClient` class with
  `chat(messages, alias, session_id) -> AsyncIterator[str]` and
  `list_aliases() -> list[str]`.
- [ ] 14. Always streams. Errors surface as the iterator yielding
  a single "⚠️ ..." string, not raising.
- [ ] 15. Tests via respx: streaming delta parsing, rate-limited
  response surface, connection-refused handling.

## Phase 3e — Streaming editor

- [ ] 16. `streaming.py`: `StreamingEditor` that edits a Telegram
  message at most every ~800ms, flushing on stream end.
- [ ] 17. Tests with a fake bot object capturing `edit_message_text`
  calls.

## Phase 3f — Handlers

- [ ] 18. `handlers.py`:
  - Allowlist check helper.
  - Text-message handler: forward to gateway with current prefs,
    stream reply back.
  - Photo handler: build multimodal content, forward.
  - Voice handler: stub reply.
  - `/session` list / `/session <id>` switch / `/session new <id>`.
  - `/model` list / `/model <alias>` switch.
  - `/start`, `/help`.
- [ ] 19. Handler tests with mocked updates and a respx-mocked
  gateway.

## Phase 3g — Bot lifecycle

- [ ] 20. `bot.py`: build `Application`, register handlers,
  `run_polling`. Graceful shutdown on SIGINT/SIGTERM (Windows
  ``CTRL_C_EVENT`` too).
- [ ] 21. `__main__.py`: load config, configure logging, build
  bot, run.

## Phase 3h — Install script

- [ ] 22. `scripts/install-telegram-bot.ps1`:
  - Assert elevated.
  - Assert NSSM + uv on PATH.
  - Run `uv sync` in `telegram-bot/` (respect `-SetupVenv` flag
    like the gateway's install script).
  - Verify bot Python can `import fitt_telegram_bot`.
  - Register `FITTTelegramBot` Windows service with auto-start,
    30s restart.
  - No firewall rule needed (the bot makes outbound HTTPS only).
- [ ] 23. `scripts/uninstall-telegram-bot.ps1` symmetric.

## Phase 3i — Open WebUI

- [ ] 24. Root-level `docker-compose.yml` with the Open WebUI
  service definition (environment refs the `FITT_BEARER_TOKEN`
  env var).
- [ ] 25. `scripts/install-open-webui.ps1`:
  - Read the Bearer token from `~/.fitt/secrets.yaml`.
  - Write a `.env` at the repo root with `FITT_BEARER_TOKEN=...`
    (gitignored already via `*.env`).
  - `docker compose up -d`.
  - Add firewall rule: inbound TCP 3000 on Private profile only.
- [ ] 26. `scripts/uninstall-open-webui.ps1`: `docker compose down
  -v`, remove firewall rule.
- [ ] 27. Add `docker-compose.yml`, `open-webui-data/`, and `.env`
  to `.gitignore`.

## Phase 3j — Documentation

- [ ] 28. Update `docs/quickstart.md`:
  - New step 8-bis: install Telegram bot (`install-telegram-bot.ps1
    -SetupVenv`) and verify `/start` in the Telegram app.
  - New step 9: install Open WebUI via `install-open-webui.ps1`
    and log in with the admin account.
- [ ] 29. Update `gateway/README.md`'s troubleshooting with the
  most likely Phase 3 issues: bot silently ignores messages
  (allowlist missing you), Open WebUI can't reach gateway (docker
  `host.docker.internal` unreachable), streaming edits rate-limited.
- [ ] 30. `telegram-bot/README.md` with the bot's own quick
  reference (commands, prefs file location, logs).
- [ ] 31. Update `.kiro/steering/project-overview.md` to mark
  Phase 3 complete after at-home verification.

## Exit criteria

- Sending `/start` from the allowlisted account returns a welcome
  message.
- Plain text message round-trips through the gateway.
- `/session new retroai` + subsequent message routes to the
  retroai session.
- `/model fitt-smart` routes to the cloud alias for subsequent
  messages in that chat.
- A photo gets a text description via the gateway's multimodal path.
- A voice note gets the stub reply.
- Open WebUI serves a chat UI at `http://<hub-tailscale-ip>:3000/`
  and its responses flow through the gateway (visible in
  `uv run fitt cost`).
- All tests pass; ruff + mypy clean.

## Non-goals (repeated from requirements)

- No STT / TTS (Phase 8).
- No tool-approval UI via Telegram (Phase 4).
- No webhook mode (Phase 3.5+).
- No Open WebUI session-to-FITT-session mapping (Phase 3.5+ if
  needed).
