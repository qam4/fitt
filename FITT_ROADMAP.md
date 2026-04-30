# FITT Roadmap

> **FITT** — *Fred Industries Two Thousand.* A self-hosted, spec-driven personal AI with persistent memory, agentic tools, and multi-interface reach. Built gradually, demoable at every phase.

This roadmap is both the outer shell **and** the draft specs. Each phase contains enough to act on: requirements, a design sketch, and a task list. When a phase begins for real, its inline section is promoted to proper Kiro spec files (`.kiro/specs/<phase>/{requirements,design,tasks}.md`) in the `home-ai-cluster` repo (or whatever the final repo name ends up being) and iterated from there.

---

## Guiding Principles

1. **Reach the inflection point fast.** The first goal is making a local LLM usable from the IDE so the LLM can help build everything else. Everything before that is manual.
2. **Spec-driven from Phase 1 onward.** Manual bootstrap stops when the LLM can follow specs. After that, every phase is written as a Kiro spec.
3. **Use mature tools; don't reinvent.** LiteLLM, Ollama, Tailscale, FastAPI, uv, MCP where they fit. Build the integration, not the components.
4. **Each phase leaves something usable.** Not "foundational work for later phases." Each phase is demoable on its own.
5. **Claude (or any cloud model) for hard turns, local for the rest.** No local-only purity. The goal is escaping *limits*, not eliminating cloud entirely.
6. **Security scales with risk.** Read-only tools: relaxed. Shell and filesystem writes: approval-gated. Home-automation and external comms: hard deny-list + audit.
7. **Models are configuration, not architecture.** LLMs improve monthly. The design never couples to a specific model — clients name logical roles (`fitt-default`, `fitt-smart`), config binds roles to current best-in-class models. Swapping is a config edit.
8. **The agent is honest about its capabilities.** When a request requires a tool the agent doesn't have, it explicitly says what's missing and recommends how to add it. Hallucinating an action, or silently producing a lesser answer when a tool would have given a better one, is a bug.
9. **Live with it before extending it.** After each phase ships, use FITT for at least two weeks before starting the next phase. Your real pain points — not the roadmap's assumptions — drive what comes next. This is the single biggest protection against building features you won't use.
10. **Shareable by construction.** No personal info, machine-specific paths, or secrets ever land in the repo. The repo contains code + `.example` templates; every user brings their own `~/.fitt/config.yaml`, `~/.fitt/secrets.yaml`, and `~/.fitt/` runtime directory. When someone else decides to try FITT, they should need only: clone, install, copy templates, fill in their values. Zero code edits to "make it about me."

---

## Phase Dependencies

```
Phase 0 (manual) ─┐
                  ├─► Phase 1 (gateway) ─► Phase 2 (memory v0)
                  │                        │
                  │                        ▼
                  │                   Phase 2.5 (sessions)
                  │                        │
                  │                        ├─► Phase 3 (telegram)
                  │                        │    │
                  │                        │    └─► Phase 8 (voice)
                  │                        │
                  │                        └─► Phase 4 (tools) ─► Phase 4.5 (project registry)
                  │                             │                     │
                  │                             │                     └─► Phase 5 (retro-ai)
                  │                             │
                  │                             ├─► Phase 6 (autonomy)
                  │                             ├─► Phase 7 (RAG memory)
                  │                             └─► Phase 9 (home assistant)
```

Phases 5, 6, 7, 9 can be done in any order once their prerequisites are met.

---

## Phase 0 — Bootstrap (Manual, ~30 minutes)

**Goal:** A local LLM reachable from the laptop's IDE. That's it.

**Why no spec:** The work is too small and too manual to benefit from a spec. Specs start when the LLM can help implement them.

**Why Ollama native, not Docker:** GPU passthrough to Docker on Windows (via WSL2 + NVIDIA Container Toolkit) is a well-known time sink. Phase 0's goal is 30 minutes to the inflection point. Native Ollama on Windows talks to the NVIDIA driver directly and Just Works. Docker buys us nothing here — no deployment target, no isolation need, and model volumes would just be bigger and harder to manage. The desktop may run Ollama in Docker later (Phase 1+) because it already needs Docker for Open WebUI, but even that is a judgment call we can make then. For the laptop: native.

**Manual steps:**
1. Install Ollama for Windows on the Predator laptop.
2. `ollama pull qwen2.5-coder:14b`
3. Verify in terminal: `ollama run qwen2.5-coder:14b`, ask it something trivial.
4. Pick an IDE. Options, in recommended order:
   - **VS Code + Continue extension** (free, open source, mature Ollama integration). Default choice.
   - **Kiro (public / external)** if it's installable at home and can be pointed at Ollama. Closest to the work experience, but verify before committing.
   - **Cursor** if you don't mind the subscription. Polished, AI-native, easy Ollama config.
5. Configure the IDE's AI provider:
   - VS Code + Continue: install the extension, open its config, set `provider: ollama`, `model: qwen2.5-coder:14b`, `apiBase: http://localhost:11434`.
   - Kiro / Cursor: whichever settings panel they expose for custom/local models, point it at `http://localhost:11434` with `qwen2.5-coder:14b`.
6. Test a chat from within the IDE — ask it to write a small function.

**Exit criteria:** You can talk to a local LLM inside your laptop's IDE, and it produces useful code responses.

**Expected experience:** Noticeably rougher than Opus. Good enough to help with boilerplate, explanations, and small refactors. Not yet good enough for complex multi-file work — that's fine; Phase 1 fixes it by routing hard turns to Claude.

---

## Phase 1 — Gateway v0 (Spec-driven, ~1 weekend) — FULL SPEC

**Goal:** A single always-on HTTP daemon that exposes an OpenAI-compatible chat endpoint and intelligently routes to Claude API or local Ollama based on the request.

### 1.1 Requirements

#### User Story 1: Unified chat endpoint

As a user, I want a single URL I can point any OpenAI-compatible client at, so that all my AI interactions flow through one place regardless of which backend ends up serving them.

**Acceptance Criteria**
- 1.1.1 The gateway exposes `POST /v1/chat/completions` on the desktop, reachable from any device on my Tailscale network.
- 1.1.2 Requests follow the OpenAI chat-completion schema (messages array, model, stream, etc.).
- 1.1.3 Streaming responses (`stream=true`) are forwarded to the client as Server-Sent Events in OpenAI format.
- 1.1.4 Non-streaming responses return the standard OpenAI JSON shape.
- 1.1.5 The endpoint is not reachable from outside Tailscale (verify with `netstat` that bindings are localhost + Tailscale interface only).

#### User Story 2: Multi-backend model routing

As a user, I want to select the model per-request so I can use Claude for hard problems and local Qwen for cheap ones.

**Acceptance Criteria**
- 2.1 Clients name *aliases* (`fitt-default`, `fitt-smart`, `fitt-fast`); the router resolves aliases to concrete models via config.
- 2.2 Passing an Anthropic-backed alias routes to the Anthropic API.
- 2.3 Passing an Ollama-backed alias routes to the configured endpoint.
- 2.4 Unknown aliases return a 400 with a list of available aliases.
- 2.5 If the primary backend for an alias is unreachable, the gateway falls back to the model's configured fallback and the response includes a header indicating the actual backend used.

#### User Story 3: Authentication

As the owner, I want the gateway to reject requests that aren't mine so that someone else on my Tailscale network can't freely spend my Anthropic credits.

**Acceptance Criteria**
- 3.1 All `/v1/*` endpoints require an `Authorization: Bearer <token>` header.
- 3.2 Valid tokens are configured in a server-side secrets file (not environment variables committed to git).
- 3.3 Invalid or missing tokens return 401.
- 3.4 Health endpoints (`/health`, `/ready`) do not require authentication.

#### User Story 4: Always-on operation

As a user, I want the gateway to survive reboots and crashes so I don't have to manually restart it.

**Acceptance Criteria**
- 4.1 The gateway runs as a Windows service (or equivalent auto-restart mechanism) on the iBUYPOWER desktop.
- 4.2 If the process crashes, it auto-restarts within 30 seconds.
- 4.3 After a full reboot, the gateway is reachable within 60 seconds without any manual steps.

#### User Story 5: Observability

As a developer, I want to see what the gateway is doing so I can debug routing and cost issues.

**Acceptance Criteria**
- 5.1 Every request is logged with: timestamp, alias, resolved model, backend chosen, latency, token counts (input/output), and — when applicable — estimated cost in USD.
- 5.2 Logs go to a rotating file in `~/.fitt/logs/gateway.log` (daily rotation, 30-day retention).
- 5.3 `GET /v1/models` returns the list of configured aliases and their current resolvability.
- 5.4 `GET /health` returns 200 if the gateway process is alive.
- 5.5 `GET /ready` returns 200 only if at least one backend for each alias is reachable.
- 5.6 A `fitt cost` CLI command summarizes current-month spend by reading the log file. (Trivial; no DB needed.)

**Not in scope for Phase 1:** A built-in server-side cost *cap*. Relying on Anthropic console's native spend limits is simpler and just as safe. If a bill surprise ever happens, Phase 10 can add enforcement. Keeping it out saves a subsystem we might never need.

### 1.2 Design

#### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│    Clients (all OpenAI-compatible, on Tailscale)            │
│    - Kiro / Continue / Cursor (IDE chat)                    │
│    - curl / httpie (testing)                                │
│    - future: Telegram bot, dashboard, MCP host              │
└────────────────────────────┬────────────────────────────────┘
                             │ HTTPS
                             │ Bearer token auth
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  FITT Gateway — FastAPI on iBUYPOWER desktop :8080          │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  Auth middleware (Bearer token)                     │    │
│  │  Request logger (with cost estimation)              │    │
│  └─────────────────────────────────────────────────────┘    │
│              │                                              │
│              ▼                                              │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  LiteLLM router (alias → model → backend)           │    │
│  │  - unified OpenAI schema                            │    │
│  │  - per-alias fallback                               │    │
│  └────────┬────────────────────┬────────────────┬──────┘    │
│           │                    │                │           │
└───────────┼────────────────────┼────────────────┼───────────┘
            │                    │                │
            ▼                    ▼                ▼
   ┌────────────────┐   ┌─────────────────┐  ┌──────────────┐
   │ Anthropic API  │   │ Ollama @ laptop │  │ Ollama @     │
   │ (Claude Sonnet,│   │ (Qwen 14B)      │  │ desktop      │
   │  Opus)         │   │ via Tailscale   │  │ (Qwen 7B)    │
   └────────────────┘   └─────────────────┘  └──────────────┘
```

#### Module design

- `gateway/app.py` — FastAPI application factory. Registers middleware and routes.
- `gateway/auth.py` — Bearer token middleware. Reads allowed tokens from `~/.fitt/secrets.yaml`.
- `gateway/router.py` — Thin wrapper around LiteLLM. Handles alias → model → backend resolution, streaming translation, fallback.
- `gateway/cost.py` — Pure function: `(model, input_tokens, output_tokens) → usd`. Used by the logger. No DB, no middleware.
- `gateway/logging_config.py` — Structured logging via `structlog` to rotating file.
- `gateway/models.py` — Pydantic models for request/response validation.
- `gateway/health.py` — Health/readiness/models endpoints.
- `gateway/cli.py` — `fitt` CLI for `fitt cost`, `fitt status`, future subcommands.

#### Configuration

`~/.fitt/config.yaml`:

```yaml
server:
  host: 0.0.0.0        # Windows Firewall restricts inbound to Tailscale interface
  port: 8080
  log_level: info

# Logical roles clients ask for. IDE, Telegram, etc. only ever name these.
# Swapping the underlying model is a config-only change.
aliases:
  fitt-default: qwen-coder-big      # everyday coding turns
  fitt-smart:   claude-sonnet       # "hard turns" escape hatch
  fitt-fast:    qwen-coder-small    # cheap classification / helpers

# Concrete models.
models:
  - id: claude-sonnet
    backend: anthropic
    model: claude-sonnet-4-5         # update when a new Claude drops
    cost_per_mtok_in: 3.00
    cost_per_mtok_out: 15.00

  - id: claude-opus
    backend: anthropic
    model: claude-opus-4-5           # optional, for truly hard turns
    cost_per_mtok_in: 15.00
    cost_per_mtok_out: 75.00

  - id: qwen-coder-big
    backend: ollama
    endpoint: http://<laptop-tailscale-ip>:11434
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

**Design rule:** no client ever hardcodes `qwen2.5-coder:14b` or similar. Clients name aliases; config binds aliases to models. Models are configuration, not architecture (Principle 7).

`~/.fitt/secrets.yaml` (mode 0600, never committed):

```yaml
anthropic_api_key: sk-ant-xxx
allowed_tokens:
  - name: personal
    token: <long random string>
```

#### Tools and dependencies

- **FastAPI** + **uvicorn**: HTTP server.
- **LiteLLM**: model routing and provider abstraction.
- **structlog**: structured logging.
- **pydantic**: request validation.
- **NSSM** or `sc.exe`: Windows service wrapper.

#### Security

- Gateway binds to `0.0.0.0`; Windows Defender Firewall rule restricts inbound 8080 to the Tailscale interface only. Verify post-install with `netstat` and an external port-scan.
- Secrets file permissions checked on startup; refuse to start if world-readable.
- Bearer tokens compared with `secrets.compare_digest` to prevent timing attacks.
- Anthropic API key never logged. Token counts and alias names logged; full prompt/response bodies not logged unless `log_bodies: true` is set for debugging.

#### Failure handling

- **Upstream 429 / 529 (Claude overloaded):** surface as 503 to client with `Retry-After` header. Don't retry automatically — let the client decide.
- **Ollama unreachable:** fall back to the alias's configured fallback. Log the failover.
- **No backend available for an alias:** return 503 with a clear error naming the alias and the attempted backends.
- **Streaming mid-response failure:** terminate the stream cleanly with an `[ERROR]` delta event; don't silently truncate.

#### Correctness properties

**Property 1 — Alias routing determinism**
*For any* request with a known alias, the resolved backend must be the primary (or configured fallback if unreachable), never any other backend.
*Validates: 2.1, 2.2, 2.3, 2.5*

**Property 2 — Auth enforcement**
*For any* `/v1/*` request without a valid Bearer token, the response is 401 and no backend call is made.
*Validates: 3.1, 3.3*

**Property 3 — Streaming passthrough fidelity**
*For any* streaming request, the event stream forwarded to the client contains the same tokens in the same order as the upstream, with only OpenAI-format envelope rewriting.
*Validates: 1.1.3*

**Property 4 — Cost logging accuracy**
*For any* completed request to a priced backend, the logged cost in USD equals `cost_per_mtok_in × input_mtokens + cost_per_mtok_out × output_mtokens` within a rounding tolerance of $0.0001.
*Validates: 5.1, 5.6*

### 1.3 Tasks

#### Phase 1a — Scaffold

- [ ] 1. Initialize `home-ai-cluster` git repo on the desktop; move `FITT_ROADMAP.md` and `RETRO_KITT_PRD.md` (rename the PRD too) into it.
- [ ] 2. Create `gateway/` Python package with `pyproject.toml`, dev dependencies (pytest, ruff, mypy), and a stub `app.py`.
- [ ] 3. Add `.gitignore` entries for `secrets.yaml`, `*.db`, `logs/`, `.venv/`.
- [ ] 4. Commit and push to private GitHub repo.

#### Phase 1b — Minimum viable endpoint

- [ ] 5. Add FastAPI + uvicorn dependencies. Implement `POST /v1/chat/completions` with a hardcoded Claude Sonnet backend.
- [ ] 6. Add `/health` and `/ready` endpoints.
- [ ] 7. Smoke test locally with `curl` + a real Anthropic API key.
- [ ] 8. Verify streaming works with `stream=true`.

#### Phase 1c — Auth

- [ ] 9. Implement Bearer-token middleware reading `~/.fitt/secrets.yaml`.
- [ ] 10. Add secrets-file permission check on startup.
- [ ] 11. Write tests for auth: missing token, wrong token, valid token.

#### Phase 1d — LiteLLM routing

- [ ] 12. Replace the hardcoded Claude backend with a LiteLLM router configured from `config.yaml`, using the alias → model pattern.
- [ ] 13. Add Ollama backends (laptop and desktop) to the config.
- [ ] 14. Implement fallback: when a primary backend is unreachable, try the model's `fallback` id. Log the failover.
- [ ] 15. Add the actual-backend header (`X-FITT-Backend`) to responses.
- [ ] 16. Write routing tests with mocked backends.
- [ ] 17. Implement failure-handling behavior documented in the design (5xx pass-through with `Retry-After`, clean stream termination on error).

#### Phase 1e — Observability

- [ ] 18. Configure structlog with rotating file handler.
- [ ] 19. Add request-logging middleware (no prompt bodies by default).
- [ ] 20. Implement cost function `gateway/cost.py` and wire into the logger.
- [ ] 21. Implement `fitt cost` CLI: tails the log, aggregates MTD USD per model, prints a summary.
- [ ] 22. Implement `GET /v1/models` returning configured aliases with current resolvability.

#### Phase 1f — Production install

- [ ] 23. Write `scripts/install-service.ps1` — registers the gateway as a Windows service via NSSM or `sc.exe`.
- [ ] 24. Set Windows Defender Firewall rules: allow inbound 8080 on Tailscale interface only.
- [ ] 25. Verify with `netstat` and an external port-scan that the gateway is not reachable from the public Wi-Fi NIC.
- [ ] 26. Set an Anthropic console spend limit on the API key (monthly cap of your choice).
- [ ] 27. Reboot the desktop; verify gateway is reachable within 60s without manual intervention.

#### Phase 1g — Wire up the IDE

- [ ] 28. On the laptop, configure Kiro / Continue / Cursor to use the gateway's Tailscale URL as an OpenAI-compatible provider.
- [ ] 29. Verify IDE chat works with `fitt-smart` and with `fitt-default`.
- [ ] 30. End-to-end smoke test: write a new spec, ask the smart alias to implement it through the gateway.

**Exit criteria:** From the laptop IDE, chat requests flow IDE → Tailscale → desktop gateway → Anthropic (for smart) or laptop Ollama (for default) → back. Gateway survives a reboot. `fitt cost` shows real spend.

**Known concerns (track as issues, don't block Phase 1):**
- Fallback is one-level. Multi-level chains deferred.
- Cost telemetry is best-effort; log-derived, not transactional.

---

## Phase 2 — Memory v0 (Spec-driven, ~1 weekend)

**Goal:** The gateway remembers. Identity (always-loaded) and today's daily history (append-only markdown) survive restarts.

**Requirements sketch:**
- Identity files in `~/.fitt/identity/{user,soul,tools}.md` injected into every system prompt.
- Daily history in `~/.fitt/history/YYYY-MM-DD.md`, append-only, one entry per turn.
- On every request, **today only** is loaded and injected between identity and the user message. (Yesterday loading defers to Phase 2.5, where session semantics settle the question of "scope of memory" properly.)
- "Keys on the counter" test: tell it a fact, restart gateway, ask about the fact same day, get the right answer.

**Design sketch:**
- New `gateway/memory.py` module.
- Memory injection happens in the request pipeline before LiteLLM dispatch.
- No vector DB. No Mem0. Plain text files only.
- Write-through: when a response completes, append `## <HH:MM> user` and `## <HH:MM> assistant` blocks to today's history file.

**Tasks sketch:**
1. Create identity templates with sensible defaults.
2. Memory-load function: read today's markdown file.
3. Inject into system message before dispatch.
4. Append-on-completion hook.
5. "Keys on the counter" integration test.

**Known concerns:**
- Today's log can hit 10k+ tokens by evening. Context budget gets tight. Phase 2.5 or Phase 7 handles compaction.
- "Memory is present" ≠ "memory is used." Claude uses context well; Qwen 14B less so. Expect the magic to be partial until Phase 7.

*Full three-file spec to be written when this phase starts.*

---

## Phase 2.5 — Sessions (Spec-driven, ~1 weekend)

**Goal:** Define what a session is, so Phases 3+ have a consistent model to build on.

**Why this phase:** Without explicit session semantics, every interface (Telegram, IDE, cron) reinvents its own scoping. MeshClaw chose "per-channel isolation"; OpenClaw chose "per-sender"; FITT needs to pick. My recommendation: **one default session shared across interfaces, with explicit sub-sessions for side projects or experiments.**

**Requirements sketch:**
- A session has an `id`, a `name`, a `created_at`, and an attached memory scope.
- Default session is `main`. Messages from any interface (Telegram DM, IDE, CLI) without an explicit session go to `main`.
- Users can create named sessions (`fitt session new retroai-debug`) and address them explicitly from the CLI or Telegram.
- Session state persists across gateway restarts.
- Each session has its own daily history file but shares identity.

**Design sketch:**
- `~/.fitt/sessions/<id>/history/YYYY-MM-DD.md` structure.
- Session resolution middleware in the gateway: picks a session based on a header, query param, or default.
- `fitt session` CLI: `list`, `new`, `rename`, `archive`.
- Memory load now reads session-scoped history.

**Tasks sketch:**
1. Session model (Pydantic) and on-disk layout.
2. Session resolution middleware.
3. CLI subcommand `fitt session`.
4. Migrate Phase 2's flat history into `main` session.
5. Tests for session isolation and default-session behavior.

**Known concerns:**
- Context switching across sessions (keywords, cwd) is deliberately deferred. v0 is "explicit or main."

*Full three-file spec to be written when this phase starts.*

---

## Phase 3 — Telegram + Browser Interface (Spec-driven, ~1 weekend)

**Goal:** Talk to FITT from your phone (Telegram) and from any browser (Open WebUI, self-hosted).

**Requirements sketch:**
- `python-telegram-bot` service alongside the gateway.
- Allowlist on your Telegram user ID.
- Open WebUI container pointed at the FITT gateway as an OpenAI-compatible provider.
- Both interfaces forward messages to the gateway, attaching the `main` session by default.
- Telegram handles text and images (forward to gateway as multimodal). Voice notes deferred to Phase 8.
- Telegram streams responses back as they arrive (edit-message pattern).
- A `/session <name>` Telegram command switches which session subsequent messages target. Open WebUI uses its own session concept mapped to FITT sessions via system prompt or header.

**Design sketch:**
- New process: `telegram-bot/` (separate service, talks to gateway via localhost HTTP).
- Open WebUI: Docker container, configured via env vars to point at the gateway.
- Both use the gateway's Bearer token to authenticate.
- One bot, one allowlisted user, many sessions.

**Tasks sketch:**
1. Register bot with BotFather, store token.
2. Telegram allowlist middleware.
3. Telegram message forwarding to gateway with session resolution.
4. Streaming reply via message edit.
5. Image passthrough.
6. `/session` Telegram commands.
7. Add Open WebUI as a Docker compose service pointed at the gateway (Tailscale-reachable).
8. Verify Open WebUI works from phone browser and desktop browser.
9. Service registration for the Telegram bot (Windows service or WSL systemd).

**Known concerns:**
- Telegram is not the approval UI yet. Phase 4 will route approval prompts back to whichever interface originated the request.
- Open WebUI has its own session abstraction that doesn't perfectly map to FITT sessions. v0 accepts some awkwardness; revisit if it gets painful.
- Open WebUI's own auth model (its signup/login) is separate from the gateway's Bearer token — lock down Open WebUI to allowlisted users independently.

*Full three-file spec to be written when this phase starts.*

---

## Phase 4 — Agentic Tools (Spec-driven, ~2 weekends)

**Goal:** The LLM can *do* things. Read files, edit files, run shell commands, with approval gates. Tools exposed via MCP where MCP servers exist; the design accepts any tool protocol the LLM can use.

**Requirements sketch:**
- MCP client in the gateway. MCP is the default protocol; the gateway abstracts tool calls so future protocols can be added without client-side changes.
- Wire in existing tool servers: filesystem, git, shell.
- Tool approval UI: routed back to the *interface that originated the request*. If the message came from Telegram, approve in Telegram; if from the IDE, approve via an OpenAI-tool-call mechanism the IDE client already understands.
- Tamper-resistant deny list (hardcoded `rm -rf /`, `git push --force`, recursive chmod, curl-piped-to-shell, etc.).
- Audit log of every tool call with HMAC chain.
- Per-tool policy in `config.yaml`: `auto` | `ask` | `trust_session` | `block`, with glob patterns.

**Capability awareness (Principle 8):**
- Every session's system prompt includes an auto-generated capabilities summary: tools currently loaded with short descriptions.
- When the agent determines a request needs a capability it doesn't have, it replies with a standard format: *what's missing, what to install or configure, a pointer to how.*
- The gateway logs declined-for-missing-capability events to `~/.fitt/capability_gaps.log` — a natural backlog of future tool additions.
- A built-in tool `list_capabilities` lets the agent enumerate tools at runtime.
- A `fitt capability-gaps` CLI prints the backlog grouped by frequency.

**Design sketch:**
- `gateway/tools.py` — MCP client bootstrap and server supervisor (restart on crash).
- `gateway/tool_registry.py` — source of truth for loaded tools, descriptions, allow rules.
- `gateway/approval.py` — approval-gate middleware, routes prompts back to originating interface.
- `gateway/audit.py` — append-only `~/.fitt/audit.jsonl` with HMAC chain; `fitt audit verify` CLI.
- `gateway/capabilities.py` — generates the capabilities section of the system prompt.
- `gateway/deny_list.py` — hardcoded deny patterns, in code not config.

**Tasks sketch:**
1. Add MCP SDK dependency and server supervisor.
2. Config schema for MCP servers.
3. Tool-call interception.
4. Approval routing back to originating interface (Telegram inline keyboard for Telegram-origin; tool-call confirmation protocol for IDE-origin).
5. Tamper-resistant deny list.
6. Audit log with HMAC chain + verify CLI.
7. Capability injection into system prompt.
8. `list_capabilities` built-in tool.
9. "Missing capability" response convention + gap-logging middleware.
10. `fitt capability-gaps` CLI.

**Known concerns:**
- Local 14B models are worse at deciding "I don't have this tool" than Claude. Phase 4 should include an eval harness that tests this specifically.
- Approval-UI across interfaces is the hairiest part. If the IDE path proves too complex, fall back to "IDE sessions auto-approve read-only; writes require escalation to Telegram." Document this explicitly.

*Full three-file spec to be written when this phase starts.*

---

## Phase 4.5 — Project Registry (Spec-driven, ~0.5 weekend)

**Goal:** FITT knows where your repos live and how to work with them. Needed by Phase 5 and gets awkward to bolt on later.

**Requirements sketch:**
- `~/.fitt/projects.yaml` lists repos FITT knows about: path, default commands, log locations, associated sessions.
- CLI: `fitt project add <path>`, `fitt project list`, `fitt project remove <name>`.
- The capability system (Phase 4) injects the known-projects list into the system prompt.
- Filesystem tool's allowed paths derive from the registry (only registered project paths are writable by default).

**Design sketch:**
- Tiny YAML schema, read at gateway startup and on file change.
- No DB. If it gets complex, revisit.

**Tasks sketch:**
1. Define schema.
2. CLI subcommand `fitt project`.
3. Hot-reload on file change (`watchdog`).
4. Inject into system prompt and filesystem tool allowlist.

*Full three-file spec to be written when this phase starts.*

---

## Phase 5 — Retro-AI Integration (~1–2 weekends)

**Goal:** Launch, monitor, and report on RL training runs via FITT.

**Key work:**
- Custom `retroai-training` MCP server: `start_training`, `get_status`, `stop_training`, `get_metrics`, `get_reward_plot`.
- Custom `emulator` MCP server: `screenshot`, `get_state`, `read_logs`.
- Completion notifications via a `telegram-out` tool (or whatever notification surface you settle on).

**Prerequisite:** Retro-AI registered in Phase 4.5.

*Full spec when the phase starts.*

---

## Phase 6 — Autonomy: Cron and Heartbeat (~1 weekend)

**Goal:** FITT acts proactively, not just reactively.

**Key work:**
- `APScheduler`-backed cron: `fitt cron add "briefing" "summarize last night" --at "0 8 * * *"`.
- Heartbeat loop reading `~/.fitt/heartbeat.md` checklist every 30 min. `HEARTBEAT_OK` as silent acknowledgment (MeshClaw pattern).
- `watchdog`-backed file triggers.
- Each scheduled or triggered invocation runs in its own session (new or named).

*Full spec when the phase starts.*

---

## Phase 7 — Memory v1: RAG, Compaction, Cross-Project (~3 weekends)

**Goal:** FITT knows your codebases and long-running conversations don't blow the context budget.

**Key work:**
- Qdrant in Docker for vector index.
- `nomic-embed-text` via Ollama for embeddings.
- Nightly re-index cron (incremental where possible).
- RAG retrieval in gateway's context-assembly step.
- **History compaction:** when today's log exceeds a threshold, summarize older blocks with an LLM pass; replace with the summary. Preserve facts, drop chatter.
- Project disambiguation (cwd heuristic, explicit switch via `/project`, keyword detection).
- Optional Mem0/Zep evaluation for fact extraction; markdown remains source of truth.

**Known concerns:**
- Embedding model changes invalidate the index. Document the re-index cost and keep embedding config stable.
- 3 weekends is honest; the hard parts are chunking strategy and project disambiguation, not the vector DB.

*Full spec when the phase starts.*

---

## Phase 8 — Voice (~2–3 weekends, optional)

**Goal:** Talk to FITT, hear it reply — especially from the watch via Telegram voice notes.

**Status:** **Optional and deferrable.** Re-evaluate priority after Phase 7. If the text experience is satisfying, voice is cosmetic; if you find yourself wanting hands-free, it earns its weight.

**Key work:**
- `faster-whisper` STT service (GPU on desktop).
- `piper-tts` or `kokoro-onnx` TTS.
- Telegram voice-note handling.
- Latency target: < 8s watch-to-watch for a 50-word reply.

**Known concerns:**
- Windows CUDA + cuDNN setup for Faster-Whisper is a real time sink.
- Piper voices are robotic; Kokoro is heavier. Plan to audition both.

*Full spec when the phase starts.*

---

## Phase 9 — Home Assistant Agent (~2–3 weekends)

**Goal:** The Alexa-like agentic half of the vision.

**Prerequisite:** A running Home Assistant instance. If one doesn't exist, add **Phase 9a: Stand up Home Assistant** (1 weekend of its own for a beginner).

**Key work:**
- Home Assistant's MCP server wired to the gateway.
- Calendar / email MCP servers.
- Hard approval gates on anything with physical-world consequences.

*Full spec when the phase starts.*

---

## Phase 10 — Hardening (Ongoing)

Not a single weekend. Never ends.

- Secondary compute node: desktop's 3070 as Ollama fallback.
- Backups: nightly snapshot of memory + audit log to NAS.
- Weekly audit log review.
- Cost-cap enforcement middleware if the Anthropic-console limit ever proves insufficient.
- Optional custom dashboard at `localhost:7777` (only if Open WebUI turns out not to cover what you want).
- Multi-project context improvements.
- Skills system if markdown grows unwieldy.
- Regression-test harness for agent behavior (record/replay common prompts across model upgrades).
- Productization decision.

---

## Testing Philosophy

FITT's architecture has two testable layers:

1. **Deterministic plumbing** (gateway routing, auth, config loading, memory file I/O, cost calculation) — covered by pytest + property tests, same discipline as chess-coaching.
2. **Agent behavior** (does FITT correctly decide when to use a tool, when to refuse, when to ask for clarification) — harder. Starting Phase 4, maintain a small harness of 10–20 prompts with expected tool-call patterns. Run it after every model swap, config change, or prompt-template edit. This is the only defense against silent quality regression when you upgrade from Qwen 2.5 → Qwen 3 → whatever.

---

## Time Estimates (honest, post-review)

| Phase | Focused time | Calendar time |
|-------|--------------|---------------|
| 0     | 30 min       | 1 day         |
| 1     | 8 hrs        | 1 weekend     |
| 2     | 5 hrs        | 1 weekend     |
| 2.5   | 6 hrs        | 1 weekend     |
| 3     | 6 hrs        | 1 weekend     |
| 4     | 14 hrs       | 2 weekends    |
| 4.5   | 3 hrs        | half weekend  |
| 5     | 10 hrs       | 1–2 weekends  |
| 6     | 6 hrs        | 1 weekend     |
| 7     | 20 hrs       | 3 weekends    |
| 8     | 14 hrs       | 2–3 weekends  |
| 9     | 16 hrs       | 2–3 weekends  |
| 10    | ongoing      | —             |

**To useful MVP (Phases 0–4):** ~6 weekends of focused work.
**To full vision (Phases 0–9):** ~15 weekends of focused work, plus 2-week "live with it" gaps between phases = 6–9 months calendar.

Expect calendar time to stretch 2–3x. Real life happens.

---

## Repo Layout (create in Phase 1)

```
home-ai-cluster/
├── README.md
├── FITT_ROADMAP.md              # this file, moved from chess-coaching repo
├── FITT_PRD.md                  # the PRD (rename from RETRO_KITT_PRD.md)
├── .kiro/
│   └── specs/
│       ├── phase1-gateway/      # promoted from inline when phase starts
│       ├── phase2-memory/
│       ├── phase2.5-sessions/
│       ├── phase3-telegram/
│       └── ...
├── gateway/                     # FastAPI daemon (Phase 1)
├── memory/                      # identity + history markdown (Phase 2)
├── telegram-bot/                # Phase 3
├── mcp-servers/                 # custom MCP server implementations (Phase 4+)
│   ├── retroai-training/
│   ├── emulator/
│   └── telegram-out/
├── skills/                      # if adopted (Phase 10)
├── configs/
│   └── config.example.yaml
└── scripts/
    └── install-service.ps1
```

---

## Open Decisions (resolve as you go)

- **Custom dashboard or stick with Open WebUI?** Open WebUI ships in Phase 3. If it covers the needs, no custom dashboard ever gets built. Re-evaluate end of Phase 4 — specifically whether approval flows, session management, and audit-log inspection warrant a purpose-built surface.
- **Fork OpenClaw/MeshClaw vs build clean?** Revisit end of Phase 3. If the gateway feels over-engineered, consider adopting an existing base.
- **Mem0/Zep or markdown forever?** Revisit end of Phase 7.
- **Shared-session vs per-interface-session?** Resolved in Phase 2.5; re-evaluate if the shared default feels wrong after living with it.
- **Productize?** Revisit end of Phase 9.

---

*Roadmap status: v1.2 — renamed to FITT, cost cap cut, sessions phase added, project registry added, voice deferred, memory simplified. When a phase begins, promote its inline section to `.kiro/specs/<phase>/{requirements,design,tasks}.md`.*
