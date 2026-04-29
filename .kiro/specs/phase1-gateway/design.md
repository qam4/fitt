# Design: FITT Phase 1 — Gateway v0

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│    Clients (all OpenAI-compatible, on Tailscale)            │
│    - VS Code + Continue (IDE chat, primary)                 │
│    - curl / httpie (smoke testing)                          │
│    - future: Telegram bot (Phase 3)                         │
│    - future: Open WebUI (Phase 3)                           │
│    - future: MCP host (Phase 4)                             │
└────────────────────────────┬────────────────────────────────┘
                             │ HTTP over Tailscale
                             │ Authorization: Bearer <token>
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  FITT Gateway — FastAPI on iBUYPOWER desktop :8080          │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  Auth middleware (Bearer token, constant-time)        │  │
│  │  Request logger (+ estimated cost)                    │  │
│  │  Error translation (429/529 → 503+Retry-After)        │  │
│  └───────────────────────────────────────────────────────┘  │
│                  │                                          │
│                  ▼                                          │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  Alias router                                         │  │
│  │  - alias → concrete model via config                  │  │
│  │  - primary + one-level fallback                       │  │
│  │  - X-FITT-Backend header on response                  │  │
│  └───────────────────────────────────────────────────────┘  │
│                  │                                          │
│                  ▼                                          │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  LiteLLM client (per-model dispatch)                  │  │
│  │  - OpenAI schema in and out                           │  │
│  │  - streaming passthrough                              │  │
│  └────────┬───────────────────┬──────────────────┬───────┘  │
│           │                   │                  │          │
└───────────┼───────────────────┼──────────────────┼──────────┘
            │                   │                  │
            ▼                   ▼                  ▼
   ┌────────────────┐   ┌─────────────────┐  ┌────────────────┐
   │ Anthropic API  │   │ Ollama @ laptop │  │ Ollama @       │
   │ (Sonnet, Opus) │   │ (Qwen 14B)      │  │ desktop        │
   │                │   │ via Tailscale   │  │ (Qwen 7B)      │
   └────────────────┘   └─────────────────┘  └────────────────┘
```

## Key Design Decisions

### Decision 1: LiteLLM as the dispatcher

LiteLLM handles the OpenAI-compatible shape in and out, and natively
supports Anthropic and Ollama. This removes the need to write per-backend
client code. Cost: an extra dependency. Benefit: when a new backend
matters (Gemini, Bedrock, vLLM), it's a config line.

Alternative considered: hit Anthropic SDK and Ollama HTTP directly. More
code, more places to fix when the OpenAI schema drifts, no real benefit.
Rejected.

### Decision 2: Aliases separate from model IDs

Clients name `fitt-default`, `fitt-smart`, `fitt-fast`. Config binds
aliases to concrete models. When Qwen 3 Coder ships, update one config
line; every client gets the upgrade.

The discipline matters: **no client ever sends `model: qwen2.5-coder:14b`
or `model: claude-sonnet-4-5` directly.** This is enforced by the router
— concrete model IDs in the request field are rejected.

### Decision 3: No database in Phase 1

The original PRD draft included a SQLite usage DB and a cost-cap
middleware. Cut. Reasons:

- Anthropic console has a built-in per-key monthly spend cap. That's the
  real safety net.
- Logs with per-request cost + a `fitt cost` CLI that reads them gives
  you visibility without a DB.
- Every subsystem is a subsystem to maintain.

Phase 10 can revisit if a bill surprise ever happens.

### Decision 4: HTTP, not HTTPS

Tailscale is the trust boundary. Every device allowed to talk to the
gateway is already authenticated at the network layer by Tailscale's
WireGuard. Adding TLS on top only matters if someone compromises a
Tailscale node, which is a different class of problem that a
gateway-layer cert doesn't meaningfully mitigate.

If we ever need TLS (e.g. for a browser that warns on HTTP), Tailscale
Serve provides it without standing up a cert authority.

### Decision 5: Windows service, not Docker, for v0

The desktop will eventually run Docker (Open WebUI in Phase 3, Qdrant in
Phase 7). But the gateway itself is a single Python process — no
isolation benefit from containerizing it, and Docker Desktop on Windows
adds a boot delay that works against "available within 60s of reboot."
Native Windows service via NSSM is simpler and faster.

Phase 10 can migrate to Docker if the service surface grows.

### Decision 6: Single-level fallback only

`fitt-default` → `qwen-coder-big` (laptop), fallback → `qwen-coder-small`
(desktop). That's it. No cascade beyond one level, no "try Claude if both
Ollamas are down." Reasons:

- The common failure mode is "laptop is asleep." One-level fallback
  handles it.
- Multi-level chains tempt implicit costly failovers (Qwen dies →
  suddenly you're spending Claude money). Explicit per-request alias
  choice is safer.

## Module Design

### `gateway/app.py` — FastAPI application factory

```python
def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="FITT Gateway", version="0.1.0")
    app.state.config = config
    app.state.router = AliasRouter(config)

    app.add_middleware(AuthMiddleware, config=config)
    app.add_middleware(RequestLoggingMiddleware, config=config)

    app.include_router(chat_router)       # /v1/chat/completions
    app.include_router(models_router)     # /v1/models
    app.include_router(health_router)     # /health, /ready

    return app
```

### `gateway/config.py` — Configuration loading

- Reads `~/.fitt/config.yaml` on startup.
- Reads `~/.fitt/secrets.yaml` on startup. Checks mode (refuses to start
  if group/world readable on POSIX; on Windows, checks that only the
  current user has read permission via `icacls`).
- Validates the alias → model graph: every alias must resolve to a
  configured model; every `fallback` must reference a configured model.
- Exposes typed config objects (`Config`, `ModelConfig`, `AliasMap`) via
  Pydantic.

### `gateway/auth.py` — Bearer token middleware

- Extracts `Authorization: Bearer <token>` from request headers.
- Compares against the allowed-token list using `secrets.compare_digest`.
- Returns 401 JSON `{"error": {"message": "invalid token", "type":
  "auth_error"}}` on failure.
- Skips `/health` and `/ready`.

### `gateway/router.py` — Alias resolution and dispatch

```python
class AliasRouter:
    def resolve(self, alias: str) -> list[ModelConfig]:
        """Returns primary and fallback model configs in order.

        Raises UnknownAlias if the alias isn't configured.
        """

    async def dispatch(
        self,
        alias: str,
        request: ChatCompletionRequest,
    ) -> ChatCompletionResponse | AsyncIterator[bytes]:
        """Try primary; on connection failure try fallback; log
        failover; tag response with X-FITT-Backend.
        """
```

Implementation uses LiteLLM's `acompletion` function per candidate model.

### `gateway/cost.py` — Pure cost function

```python
def estimate_cost(
    model: ModelConfig,
    input_tokens: int,
    output_tokens: int,
) -> Decimal:
    """Returns USD cost. Local Ollama models return Decimal('0')."""
```

Used by the logger. Never written to disk separately — the log is the DB.

### `gateway/logging_config.py` — Structured logging

- `structlog` with JSON renderer.
- Rotating file handler: daily, 30-day retention.
- Log schema per request:
  ```json
  {
    "ts": "2026-04-29T20:12:34Z",
    "event": "chat.completion",
    "alias": "fitt-smart",
    "model": "claude-sonnet-4-5",
    "backend": "anthropic",
    "backend_actual": "anthropic",
    "latency_ms": 1420,
    "input_tokens": 532,
    "output_tokens": 284,
    "cost_usd": 0.00586,
    "status": "ok"
  }
  ```
- No prompt/response bodies unless `log_bodies: true`.

### `gateway/chat.py` — The `/v1/chat/completions` endpoint

Thin handler that:
1. Validates request against OpenAI schema (Pydantic).
2. Rejects requests where `model` is not a configured alias.
3. Calls `AliasRouter.dispatch`.
4. Returns streaming or non-streaming response based on `stream`.
5. Translates upstream 429/529 → 503 + `Retry-After`.

### `gateway/health.py` — Health and readiness

- `GET /health` — alive check (no backend touch).
- `GET /ready` — probes one reachability check per alias; returns 200
  only if every alias has at least one reachable backend.
- `GET /v1/models` — returns aliases + current resolvability.

### `gateway/cli.py` — `fitt` command

v0 subcommands:
- `fitt cost` — parse `~/.fitt/logs/gateway.log*`, aggregate MTD USD per
  model, print table.
- `fitt status` — hit `/v1/models` on localhost, print a table.
- `fitt config check` — validate `config.yaml` and `secrets.yaml`
  without starting the server.

## Configuration Format

`~/.fitt/config.yaml`:

```yaml
server:
  host: 0.0.0.0              # Windows Firewall restricts inbound to Tailscale
  port: 8080
  log_level: info
  log_bodies: false          # flip to true for debug

aliases:
  fitt-default: qwen-coder-big
  fitt-smart:   claude-sonnet
  fitt-fast:    qwen-coder-small

models:
  - id: claude-sonnet
    backend: anthropic
    model: claude-sonnet-4-5
    cost_per_mtok_in:  3.00
    cost_per_mtok_out: 15.00

  - id: claude-opus
    backend: anthropic
    model: claude-opus-4-5
    cost_per_mtok_in:  15.00
    cost_per_mtok_out: 75.00

  - id: qwen-coder-big
    backend: ollama
    endpoint: http://100.x.y.z:11434   # laptop Tailscale IP
    model: qwen2.5-coder:14b
    fallback: qwen-coder-small

  - id: qwen-coder-small
    backend: ollama
    endpoint: http://localhost:11434
    model: qwen2.5-coder:7b

logging:
  dir: ~/.fitt/logs
  retention_days: 30
```

`~/.fitt/secrets.yaml` (mode 0600, never in git):

```yaml
anthropic_api_key: sk-ant-xxx
allowed_tokens:
  - name: personal
    token: <long random string, 32+ chars>
```

## Tools and Dependencies

- **FastAPI** + **uvicorn** — HTTP server.
- **LiteLLM** — multi-provider dispatcher.
- **httpx** — HTTP client (used by LiteLLM, and by health checks).
- **pydantic** — request/config validation.
- **structlog** — structured logging.
- **pyyaml** — config parsing.
- **click** — `fitt` CLI.
- **NSSM** (external binary) — Windows service wrapper.

Dev:
- **pytest** + **pytest-asyncio** — tests.
- **httpx.AsyncClient** — test client.
- **respx** — HTTP mocking for backend tests.
- **ruff** — lint and format.
- **mypy** — type checking.

## Security

- Tailscale is the network perimeter. Gateway binds to `0.0.0.0` but
  Windows Defender Firewall allows inbound 8080 only on the Tailscale
  interface. Verified with `netstat -an` showing no bind on the
  public Wi-Fi NIC IP.
- `secrets.yaml` permissions checked on startup. On Windows, use
  `icacls` to verify only the current user has read. Refuse to start
  on failure.
- Bearer tokens: 32+ characters, generated with `secrets.token_urlsafe`.
  Compared with `secrets.compare_digest`.
- Anthropic API key never logged. Token counts and alias names logged;
  bodies only with explicit opt-in.
- Anthropic spend cap set in the Anthropic console on the API key — the
  authoritative limit.

## Failure Handling (explicit behaviors)

| Upstream behavior | Gateway response |
|---|---|
| Backend HTTP 200 stream | Passthrough as SSE |
| Backend HTTP 200 non-stream | Passthrough as JSON |
| Backend connection refused / timeout | Try fallback; if fallback also fails, 503 with body |
| Backend HTTP 429 (rate limited) | 503 + `Retry-After: <seconds>` |
| Backend HTTP 529 (Claude overloaded) | 503 + `Retry-After: 30` |
| Backend HTTP 5xx (other) | 502 + body preserving upstream message |
| Backend HTTP 4xx (bad request) | Pass through with same status |
| Mid-stream connection drop | Terminate stream with `[ERROR]` SSE event |
| Unknown alias | 400 + body listing available aliases |
| Invalid auth | 401 + body, no backend call |
| Model id instead of alias | 400 + body explaining alias requirement |

## Correctness Properties

### Property 1: Alias routing determinism

*For any* request with a known alias, the dispatched backend is either
the alias's configured primary or the configured fallback (when primary
is unreachable). No other backend is ever called.

**Validates:** 2.1, 2.2, 2.3, 2.5

### Property 2: Auth enforcement

*For any* `/v1/*` request without a valid Bearer token, the response is
401 and no backend call is made. Verified by asserting the mock
upstream was not invoked.

**Validates:** 3.1, 3.3

### Property 3: Streaming passthrough fidelity

*For any* streaming request, the sequence of tokens delivered to the
client equals the sequence emitted by the upstream, with only OpenAI
envelope rewriting.

**Validates:** 1.3

### Property 4: Cost logging accuracy

*For any* completed request to a priced backend, the logged `cost_usd`
equals
`(input_tokens / 1e6) * cost_per_mtok_in + (output_tokens / 1e6) * cost_per_mtok_out`
within a rounding tolerance of $0.0001.

**Validates:** 5.1, 5.7

### Property 5: Alias-only model names

*For any* request whose `model` field is a concrete model id rather than
a configured alias, the response is 400 and no backend call is made.

**Validates:** 2.6

### Property 6: No-leak on fallback

*For any* failover from primary to fallback, the response's
`X-FITT-Backend` header names the *actual* backend that served the
request, not the primary.

**Validates:** 2.5

## Testing Strategy

### Unit Tests

- `test_config_loads_valid_yaml` — valid config parses.
- `test_config_rejects_missing_alias_target` — alias pointing at
  non-existent model rejected.
- `test_config_rejects_missing_fallback_target` — fallback pointing at
  non-existent model rejected.
- `test_secrets_file_mode_refuses_world_readable` — Windows and POSIX.
- `test_cost_calculation` — known token counts → expected USD.
- `test_cost_ollama_is_zero` — Ollama models always return 0.
- `test_alias_resolve_unknown_raises` — raises `UnknownAlias`.
- `test_auth_accepts_valid_token` — 200.
- `test_auth_rejects_missing_token` — 401.
- `test_auth_rejects_wrong_token` — 401, no backend call.
- `test_auth_skips_health_endpoints` — /health, /ready work without
  token.
- `test_chat_rejects_model_id_as_alias` — 400.
- `test_chat_rejects_unknown_alias` — 400 with available-aliases list.

### Integration Tests (with mocked upstreams via `respx`)

- `test_chat_routes_anthropic_alias_to_anthropic` — mock Anthropic,
  verify hit.
- `test_chat_routes_ollama_alias_to_ollama` — mock Ollama, verify hit.
- `test_chat_primary_unreachable_falls_back` — primary 503, fallback
  200; response has `X-FITT-Backend` = fallback.
- `test_chat_both_unreachable_returns_503` — both 503; response 503.
- `test_chat_upstream_429_returns_503_with_retry_after` — Anthropic
  returns 429; gateway returns 503 + Retry-After.
- `test_chat_streaming_passthrough` — mock streaming; verify byte
  sequence.
- `test_chat_stream_mid_failure_emits_error_event` — upstream drops
  mid-stream; verify `[ERROR]` SSE event.
- `test_health_200_when_alive` — always 200.
- `test_ready_200_when_all_aliases_resolvable` — mocks all reachable.
- `test_ready_503_when_any_alias_unresolvable` — mocks one down.

### Property Tests (`hypothesis`, 100+ iterations each)

- **Phase 1, Property 1: Alias routing determinism** — generate random
  valid alias names; assert dispatched backend ∈ {primary, fallback}.
- **Phase 1, Property 4: Cost calculation** — generate random
  (input_tokens, output_tokens, rates); assert computed cost matches
  closed-form formula.

### Manual / Smoke Tests (post-install)

- From laptop: `curl -H "Authorization: Bearer <token>" ...` — round-trip
  a real Claude call, a real Ollama call, and verify the
  `X-FITT-Backend` header.
- From phone (Tailscale): same `curl`, confirm reachability over the
  mesh.
- External port scan from outside Tailscale: confirm 8080 closed.
- Reboot desktop; time to first successful request < 60s.
- Kill the gateway process; verify auto-restart within 30s.

## Known Concerns (tracked, not blocking)

- **Single-level fallback.** Cascaded fallback chains deferred to a
  later phase if real-world use demands them.
- **Cost telemetry is log-derived, not transactional.** A crashed
  `fitt cost` midway through aggregation just means re-run. Good enough.
- **LiteLLM version churn.** LiteLLM breaks its own API periodically.
  Pin the version in `pyproject.toml` and upgrade deliberately.
- **Windows service install is manual.** The `install-service.ps1`
  script automates it but still requires admin and NSSM pre-installed.

## Future Extensions (explicit non-goals for Phase 1)

- Memory injection (Phase 2).
- Session semantics (Phase 2.5).
- Telegram frontend (Phase 3).
- Open WebUI frontend (Phase 3).
- MCP tool calling (Phase 4).
- RAG over repos (Phase 7).
- Cost-cap middleware (Phase 10, only if needed).
