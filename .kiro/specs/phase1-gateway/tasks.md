# Tasks: FITT Phase 1 — Gateway v0

**Status:** shipped

## Phase 1a — Repo and Scaffold

- [x] 1. Create private GitHub repo `home-ai-cluster`. On the desktop,
  clone it locally.
- [x] 2. Move `FITT_ROADMAP.md` and the PRD (rename to `FITT_PRD.md`)
  from the chess-coaching workspace into the new repo root.
- [x] 3. Copy `.kiro/specs/phase1-gateway/` from the chess-coaching
  workspace into the new repo.
- [x] 4. Create repo README with a description and pointers to the
  roadmap and specs.
- [x] 5. Create the `gateway/` Python package:
  - `pyproject.toml` with project metadata.
  - Dependencies: `fastapi`, `uvicorn[standard]`, `litellm`, `httpx`,
    `pydantic`, `pydantic-settings`, `structlog`, `pyyaml`, `click`,
    `rich`.
  - Dev dependencies: `pytest`, `pytest-asyncio`, `respx`, `hypothesis`,
    `ruff`, `mypy`.
  - `pytest` and `ruff` configuration.
- [x] 6. Create `gateway/__init__.py` with `__version__ = "0.1.0"`.
- [x] 7. Create `tests/` directory with `conftest.py` and a smoke test
  that imports `gateway`.
- [x] 8. `.gitignore` entries in place: `secrets.yaml`, `*.db`,
  `logs/`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`,
  etc.
- [x] 9. Verify `uv sync` creates the venv and `uv run pytest` runs the
  suite green.
- [x] 10. Commit and push the scaffold.

## Phase 1b — Configuration

- [x] 11. Create `gateway/config.py` with Pydantic models:
  `ServerConfig`, `ModelConfig`, `LoggingConfig`, `Config`,
  `AllowedToken`, `TelegramSecrets`, `Secrets`.
- [x] 12. Implement `load_config(config_path, secrets_path)` that reads
  both YAML files, validates the alias→model graph, and returns a typed
  `Config` object.
- [x] 13. Implement `_check_secrets_permissions(path)` that refuses to
  load if the file is group/world-readable on POSIX. Windows check is a
  best-effort no-op (documented in docs/quickstart.md).
- [x] 14. Create `configs/config.example.yaml` (OpenRouter primary,
  Anthropic commented, laptop + desktop Ollama).
- [x] 15. Create `configs/secrets.example.yaml` with placeholders
  (OpenRouter key required, Anthropic commented, Telegram commented).
- [x] 16. Config tests pass: valid YAML loads, missing alias target
  rejected, missing fallback rejected, self-fallback rejected, ollama
  without endpoint rejected, secrets permission checks.

## Phase 1c — Cost function

- [x] 17. Create `gateway/cost.py` with `estimate_cost(model,
  input_tokens, output_tokens) -> Decimal`.
- [x] 18. Ollama models return `Decimal('0')`.
- [x] 19. Priced cloud models compute from `cost_per_mtok_in` and
  `cost_per_mtok_out`.
- [x] 20. Unit tests for both branches pass (including negative-token
  clamp and free-model zero).
- [x] 21. Property test **Phase 1, Property 4: Cost calculation**
  passes (200 iterations).

## Phase 1d — Logging

- [x] 22. Create `gateway/logging_config.py`:
  - structlog with JSON renderer for file, ConsoleRenderer for stderr.
  - Rotating file handler in `<logging.dir>/gateway.log`, daily
    rotation, configurable retention.
- [x] 23. Create `log_request(logger, **kwargs)` helper.
- [x] 24. Tests pass: log file created, JSON-parseable entries,
  Decimal→string preserved, idempotent configure.

## Phase 1e — Auth middleware

- [x] 25. Create `gateway/auth.py` with `AuthMiddleware`:
  - Bearer extraction, `secrets.compare_digest`, 401 JSON on failure,
    exempts /health /ready /v1/models.
- [x] 26. Tests pass: missing token, wrong token, malformed header,
  empty bearer, valid token passes through, exempt paths skipped.

## Phase 1f — Alias router

- [x] 27. Create `gateway/router.py` with `AliasRouter`:
  - `resolve(alias)` returns primary + optional fallback.
  - `dispatch(alias, body)` tries primary, then fallback on transport
    failure, raises `NoBackendAvailable` if both fail.
- [x] 28. Wrap `litellm.acompletion`; streaming and non-streaming
  supported.
- [x] 29. `backend_tag(model)` string for the `X-FITT-Backend` header.
- [x] 30. Tests pass: cloud routing, ollama routing, fallback on
  `httpx.ConnectError`, 503 when all fail, semantic errors not retried.
- [x] 31. Property test **Phase 1, Property 1: Alias routing
  determinism** passes.

## Phase 1g — Chat endpoint

- [x] 32. Create `gateway/models.py` with permissive OpenAI-compatible
  Pydantic request model.
- [x] 33. Create `gateway/chat.py` implementing `POST
  /v1/chat/completions`:
  - Validates request, rejects concrete model ids.
  - Calls `AliasRouter.dispatch`.
  - Streams via SSE or returns JSON.
  - Sets `X-FITT-Backend`, `X-FITT-Alias`, `X-FITT-Fallback` headers.
  - Translates upstream 429/529 to 503+Retry-After, 4xx through, other
    5xx to 502.
  - Logs each request with latency, tokens, cost.
- [x] 34. Tests pass: unknown alias 400, concrete model id 400, missing
  messages 422, cloud routing, ollama routing, fallback + header
  fidelity, both unreachable 503, upstream 429/529 translation, SSE
  passthrough in order, mid-stream failure emits `[ERROR]` event.
- [x] 35. Property coverage: **Property 5 (alias-only)** via explicit
  and varied-input tests; **Property 6 (no-leak on fallback)** via
  fallback tests asserting `X-FITT-Backend` matches actual backend.

## Phase 1h — Models and health endpoints

- [x] 36. Create `gateway/health.py` (/health, /ready) and
  `gateway/models_endpoint.py` (/v1/models).
- [x] 37. Tests pass: /health always 200, /v1/models lists aliases with
  fitt_backend extension, /ready 200 when all reachable, 503 with
  failing-aliases list when degraded.

## Phase 1i — Application factory and entry point

- [x] 38. Create `gateway/app.py::create_app(config)` that registers
  middleware, routers, and exception handlers for `UnknownAlias`,
  `ModelIdNotAlias`, `NoBackendAvailable`.
- [x] 39. Create `gateway/__main__.py` plus `fitt-gateway` console
  script. Loads config, configures logging, runs uvicorn.
- [x] 40. Smoke test at work (in-sandbox) passed:
  - Gateway boots via `python -m gateway` with FITT_HOME override.
  - `curl /health` → 200.
  - `curl /v1/models` → correct JSON with aliases and extensions.
  - Missing/wrong Bearer → 401.
  - Real-upstream tests deferred to at-home smoke (OpenRouter + Ollama
    reachability).

## Phase 1j — `fitt` CLI

- [x] 41. Create `gateway/cli.py` with `click` command group:
  - `fitt cost` — parses `~/.fitt/logs/gateway.log*`, aggregates MTD
    USD per model, prints Rich table.
  - `fitt status` — GETs `/v1/models` and `/ready`, prints a table.
  - `fitt config check` — loads + validates config/secrets without
    starting the server.
- [x] 42. Added `fitt` console script entry in `pyproject.toml`.
- [x] 43. Tests pass: cost aggregation filters by month, handles
  no-logs case, config-check rejects invalid config.

## Phase 1k — Production install (desktop, at-home)

- [x] 44. Write `scripts/install-service.ps1`:
  - Installs NSSM if missing.
  - Registers the gateway as `FITTGateway` Windows service.
  - Sets auto-start, 30-second restart on failure.
- [x] 45. Write `scripts/uninstall-service.ps1` for symmetry.
- [x] 46. Firewall rule creation automated in the install script
  (inbound TCP 8080 on Private profile only).
- [ ] 47. Verify with `netstat -an` that 8080 is bound on the Tailscale
  IP and not the public Wi-Fi NIC IP. (At-home, runtime.)
- [ ] 48. Run an external port scan (from the phone's mobile data,
  outside Tailscale) against the desktop's public IP; confirm 8080
  closed. (At-home, runtime.)
- [ ] 49. Set a per-provider spend cap in the provider dashboard
  (OpenRouter credit balance as a hard ceiling; Anthropic console spend
  limit if/when direct Anthropic is enabled).
- [ ] 50. Reboot desktop; verify gateway reachable within 60s without
  manual steps. (At-home, runtime.)
- [ ] 51. Kill the Python process; verify auto-restart within 30s.
  (At-home, runtime.)

## Phase 1l — IDE wiring and end-to-end (at-home)

- [ ] 52. On the laptop, configure VS Code + Continue:
  - Add a custom OpenAI-compatible provider.
  - URL: `http://<desktop-tailscale-ip>:8080`.
  - API key: the Bearer token.
  - Models: `fitt-default`, `fitt-smart`, `fitt-fast`.
- [ ] 53. In Continue's chat, send a short message using `fitt-smart`
  → verify the cloud model responds, check `X-FITT-Backend: openrouter`
  (or the configured cloud backend).
- [ ] 54. Send a message using `fitt-default` → verify Qwen responds
  via laptop Ollama.
- [ ] 55. Disable Ollama on the laptop (`ollama stop`); send
  `fitt-default` again → verify failover to desktop Ollama, with
  `X-FITT-Backend: ollama` (pointing at the desktop endpoint).
- [ ] 56. Run `fitt cost` on the desktop → verify MTD spend reflects
  the cloud calls made during testing.

## Phase 1m — Documentation and release

- [x] 57. Flesh out `gateway/README.md`:
  - Installation steps (Windows service, config files, firewall).
  - Configuration reference (aliases, models, cost rates).
  - `fitt` CLI reference.
  - HTTP API reference.
  - Failure-handling table.
  - Troubleshooting section (auth 401, /ready 503, streaming cost=0,
    service crash loop, update workflow).
- [x] 57a. Consolidated setup docs into `docs/quickstart.md` - one
  end-to-end guide covering Tailscale, Ollama + `OLLAMA_HOST=0.0.0.0`,
  uv, NSSM, accounts, secrets, service install, and IDE wiring.
- [ ] 58. Update `FITT_ROADMAP.md`: mark Phase 1 complete, note any
  deviations from the spec. (After at-home smoke.)
- [ ] 59. Commit and push. Tag the repo `v0.1.0-phase1`. (After
  at-home smoke.)

## Exit Criteria

- From the laptop IDE, a chat request using `fitt-smart` flows: IDE
  → Tailscale → desktop gateway → OpenRouter → back. Streaming works.
- A request using `fitt-default` hits the laptop Ollama.
- Ollama down on laptop → automatic fallback to desktop Ollama, header
  reflects reality.
- Gateway survives a full desktop reboot with no manual steps.
- `fitt cost` shows real spend.
- External port scan confirms 8080 not exposed outside Tailscale.
- All tests pass; ruff and mypy clean.
- No personal values committed to the repo; `secrets.yaml` and user
  `config.yaml` live only in `~/.fitt/`.

## Non-Goals (repeated from requirements)

- No memory (Phase 2).
- No sessions (Phase 2.5).
- No Telegram (Phase 3).
- No Open WebUI (Phase 3).
- No MCP tools (Phase 4).
- No cost-cap middleware (Phase 10, if ever).
- No HTTPS with real certs (Tailscale covers network trust).
- No Docker for the gateway itself (Windows service).
