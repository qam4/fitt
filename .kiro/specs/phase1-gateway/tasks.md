# Tasks: FITT Phase 1 — Gateway v0

## Phase 1a — Repo and Scaffold

- [ ] 1. Create private GitHub repo `home-ai-cluster` (or chosen name).
  On the desktop, clone it locally.
- [ ] 2. Move `FITT_ROADMAP.md` and the PRD (rename to `FITT_PRD.md`)
  from the chess-coaching workspace into the new repo root.
- [ ] 3. Copy `.kiro/specs/phase1-gateway/` from the chess-coaching
  workspace into the new repo.
- [ ] 4. Create repo README with one-line description and a pointer to
  `FITT_ROADMAP.md`.
- [ ] 5. Create the `gateway/` Python package:
  - `pyproject.toml` with project metadata.
  - Dependencies: `fastapi`, `uvicorn[standard]`, `litellm`, `httpx`,
    `pydantic`, `pydantic-settings`, `structlog`, `pyyaml`, `click`.
  - Dev dependencies: `pytest`, `pytest-asyncio`, `respx`, `hypothesis`,
    `ruff`, `mypy`.
  - `pytest` and `ruff` configuration.
- [ ] 6. Create `gateway/__init__.py` with `__version__ = "0.1.0"`.
- [ ] 7. Create `tests/` directory with `conftest.py` and a smoke test
  that imports `gateway`.
- [ ] 8. Add `.gitignore` entries: `~/.fitt`, `secrets.yaml`, `*.db`,
  `logs/`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`.
- [ ] 9. Verify `pip install -e ".[dev]"` works and `pytest` runs the
  smoke test green.
- [ ] 10. Commit and push the scaffold.

## Phase 1b — Configuration

- [ ] 11. Create `gateway/config.py` with Pydantic models: `ServerConfig`,
  `ModelConfig`, `AliasMap`, `LoggingConfig`, `Config`, `Secrets`.
- [ ] 12. Implement `Config.load(config_path, secrets_path)` that reads
  both YAML files, validates the alias→model graph, and returns a typed
  `Config` object.
- [ ] 13. Implement `Secrets.check_permissions(path)` that refuses to
  load if the file is world-readable (cross-platform: POSIX mode bits,
  Windows ACL check via `icacls`).
- [ ] 14. Create `configs/config.example.yaml` with a complete example
  (aliases, Claude + two Ollama models, logging).
- [ ] 15. Create `configs/secrets.example.yaml` with placeholders and
  an instruction comment ("copy to ~/.fitt/secrets.yaml and restrict
  permissions").
- [ ] 16. Write tests:
  - `test_config_loads_valid_yaml`
  - `test_config_rejects_missing_alias_target`
  - `test_config_rejects_missing_fallback_target`
  - `test_secrets_rejects_world_readable`

## Phase 1c — Cost function

- [ ] 17. Create `gateway/cost.py` with `estimate_cost(model,
  input_tokens, output_tokens) -> Decimal`.
- [ ] 18. Ollama models (backend=="ollama") return `Decimal('0')`.
- [ ] 19. Anthropic models compute from `cost_per_mtok_in` and
  `cost_per_mtok_out`.
- [ ] 20. Write unit tests for both branches.
- [ ] 21. Write property test:
  - **Phase 1, Property 4: Cost calculation** — hypothesis-generated
    token counts and rates; assert formula matches.

## Phase 1d — Logging

- [ ] 22. Create `gateway/logging_config.py`:
  - Configure `structlog` with JSON renderer.
  - Rotating file handler in `~/.fitt/logs/gateway.log`, daily rotation,
    configurable retention.
  - Console handler at info+ for dev.
- [ ] 23. Create a `log_request(event, **kwargs)` helper that emits the
  standard request log schema.
- [ ] 24. Write test: log file is created, one log entry is valid JSON.

## Phase 1e — Auth middleware

- [ ] 25. Create `gateway/auth.py` with `AuthMiddleware`:
  - Extract `Authorization: Bearer <token>`.
  - Compare against `config.secrets.allowed_tokens` with
    `secrets.compare_digest`.
  - 401 JSON on failure.
  - Skip for `/health`, `/ready`, and `/v1/models`.
- [ ] 26. Write tests:
  - `test_auth_accepts_valid_token`
  - `test_auth_rejects_missing_token`
  - `test_auth_rejects_wrong_token`
  - `test_auth_skips_health_endpoints`
  - Verify mock upstream is not invoked on 401.

## Phase 1f — Alias router

- [ ] 27. Create `gateway/router.py` with `AliasRouter`:
  - `resolve(alias) -> list[ModelConfig]` returns `[primary,
    fallback?]`. Raises `UnknownAlias` if not configured.
  - `async dispatch(alias, request)` tries primary via LiteLLM; on
    connection failure tries fallback; raises `NoBackendAvailable` if
    both fail.
- [ ] 28. Wrap LiteLLM's `acompletion` for non-streaming and streaming
  (use `acompletion(..., stream=True)` returning async iterator).
- [ ] 29. Capture the backend actually used and propagate it so the
  handler can set `X-FITT-Backend`.
- [ ] 30. Write tests with `respx` mocks:
  - `test_alias_resolve_unknown_raises`
  - `test_dispatch_routes_anthropic`
  - `test_dispatch_routes_ollama`
  - `test_dispatch_falls_back_on_connection_error`
  - `test_dispatch_raises_when_all_fail`
- [ ] 31. Write property test:
  - **Phase 1, Property 1: Alias routing determinism** — hypothesis-
    generated alias names over a valid config; assert dispatched backend
    ∈ {primary, fallback}.

## Phase 1g — Chat endpoint

- [ ] 32. Create `gateway/models.py` with Pydantic models mirroring the
  OpenAI chat-completion schema (request, response, streaming chunk).
- [ ] 33. Create `gateway/chat.py` implementing `POST
  /v1/chat/completions`:
  - Validate request against schema.
  - Reject concrete model ids (must be alias) with 400.
  - Call `AliasRouter.dispatch`.
  - Stream or return JSON based on `stream` field.
  - Set `X-FITT-Backend` header.
  - Translate upstream errors per the table in design.md § Failure
    Handling.
- [ ] 34. Write integration tests:
  - `test_chat_rejects_model_id_as_alias`
  - `test_chat_rejects_unknown_alias`
  - `test_chat_routes_anthropic_alias_to_anthropic`
  - `test_chat_routes_ollama_alias_to_ollama`
  - `test_chat_primary_unreachable_falls_back`
  - `test_chat_both_unreachable_returns_503`
  - `test_chat_upstream_429_returns_503_with_retry_after`
  - `test_chat_streaming_passthrough`
  - `test_chat_stream_mid_failure_emits_error_event`
- [ ] 35. Write property tests:
  - **Phase 1, Property 5: Alias-only model names** — hypothesis-
    generated concrete model ids; assert 400.
  - **Phase 1, Property 6: No-leak on fallback** — assert
    `X-FITT-Backend` matches actual backend across all fallback cases.

## Phase 1h — Models and health endpoints

- [ ] 36. Create `gateway/health.py`:
  - `GET /health` — always 200.
  - `GET /ready` — for each alias, attempt to reach its primary or
    fallback (short timeout); return 200 if every alias has at least one
    reachable backend, else 503 with the failing aliases.
  - `GET /v1/models` — return aliases with current resolvability.
- [ ] 37. Write tests:
  - `test_health_200`
  - `test_ready_200_when_all_reachable`
  - `test_ready_503_when_one_unreachable`
  - `test_models_lists_aliases`

## Phase 1i — Application factory and entry point

- [ ] 38. Create `gateway/app.py::create_app(config)` that registers
  middleware, routers, and exception handlers.
- [ ] 39. Create `gateway/__main__.py` and a `fitt-gateway` console
  script entry in `pyproject.toml` that loads config, creates the app,
  and runs uvicorn.
- [ ] 40. Manual smoke test on the desktop:
  - `fitt-gateway` starts on :8080.
  - `curl http://localhost:8080/health` → 200.
  - `curl -H "Authorization: Bearer <token>" -X POST
    http://localhost:8080/v1/chat/completions -d '{"model":
    "fitt-smart", "messages": [{"role":"user","content":"say hi"}]}'`
    returns a real Claude response.
  - Repeat with `fitt-default` → real Qwen response.

## Phase 1j — `fitt` CLI

- [ ] 41. Create `gateway/cli.py` with `click` command group:
  - `fitt cost` — parses `~/.fitt/logs/gateway.log*`, aggregates MTD
    USD per model, prints a table.
  - `fitt status` — GETs `/v1/models` on localhost, prints a table.
  - `fitt config check` — loads config + secrets, prints errors, does
    not start the server.
- [ ] 42. Add `fitt` console script entry in `pyproject.toml`.
- [ ] 43. Write tests:
  - `test_cli_cost_aggregates_from_log`
  - `test_cli_config_check_rejects_bad_config`

## Phase 1k — Production install (desktop)

- [ ] 44. Write `scripts/install-service.ps1`:
  - Installs NSSM if missing.
  - Registers the gateway as `FITTGateway` Windows service.
  - Sets auto-start, 30-second restart on failure.
  - Configures service to run as a non-admin user.
- [ ] 45. Write `scripts/uninstall-service.ps1` for symmetry.
- [ ] 46. Add Windows Defender Firewall rule allowing inbound 8080
  only on the Tailscale network profile. Document the exact command in
  the install script.
- [ ] 47. Verify with `netstat -an` that 8080 is bound on the Tailscale
  IP and not the public Wi-Fi NIC IP.
- [ ] 48. Run an external port scan (from the phone's mobile data,
  outside Tailscale) against the desktop's public IP; confirm 8080
  closed.
- [ ] 49. Set an Anthropic-console spend cap on the API key (the
  authoritative limit).
- [ ] 50. Reboot desktop; verify gateway reachable within 60s without
  manual steps.
- [ ] 51. Kill the Python process; verify auto-restart within 30s.

## Phase 1l — IDE wiring and end-to-end

- [ ] 52. On the laptop, configure VS Code + Continue:
  - Add a custom OpenAI-compatible provider.
  - URL: `http://<desktop-tailscale-ip>:8080`.
  - API key: the Bearer token.
  - Models: `fitt-default`, `fitt-smart`, `fitt-fast`.
- [ ] 53. In Continue's chat, send a short message using `fitt-smart`
  → verify Claude responds, check `X-FITT-Backend: anthropic` in the
  network tab.
- [ ] 54. Send a message using `fitt-default` → verify Qwen responds
  via laptop Ollama.
- [ ] 55. Disable Ollama on the laptop (`ollama stop`); send
  `fitt-default` again → verify failover to desktop Ollama, with
  `X-FITT-Backend: ollama-desktop` (or equivalent).
- [ ] 56. Run `fitt cost` on the desktop → verify MTD spend reflects the
  Claude calls made during testing.

## Phase 1m — Documentation

- [ ] 57. Write `gateway/README.md` with:
  - Installation steps (Windows service, config files, firewall).
  - Configuration reference (aliases, models, cost rates).
  - `fitt` CLI reference.
  - Failure-handling table (copy from design.md).
  - Troubleshooting (common issues: auth 401, 503 on /ready, firewall
    not allowing Tailscale).
- [ ] 58. Update `FITT_ROADMAP.md`: mark Phase 1 complete, note any
  deviations from the spec.
- [ ] 59. Commit and push. Tag the repo `v0.1.0-phase1`.

## Exit Criteria

- From the laptop IDE, a chat request using `fitt-smart` flows: IDE
  → Tailscale → desktop gateway → Anthropic → back. Streaming works.
- A request using `fitt-default` hits the laptop Ollama.
- Ollama down on laptop → automatic fallback to desktop Ollama, header
  reflects reality.
- Gateway survives a full desktop reboot with no manual steps.
- `fitt cost` shows real spend.
- External port scan confirms 8080 not exposed outside Tailscale.
- All tests pass; ruff and mypy clean.

## Non-Goals (repeated from requirements)

- No memory (Phase 2).
- No sessions (Phase 2.5).
- No Telegram (Phase 3).
- No Open WebUI (Phase 3).
- No MCP tools (Phase 4).
- No cost-cap middleware (Phase 10, if ever).
- No HTTPS with real certs (Tailscale covers network trust).
- No Docker for the gateway itself (Windows service).
