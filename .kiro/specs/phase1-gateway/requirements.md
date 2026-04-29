# Requirements: FITT Phase 1 — Gateway v0

## Overview

The FITT Gateway is an always-on HTTP daemon running on the iBUYPOWER
desktop. It exposes a single OpenAI-compatible chat endpoint that any
OpenAI-compatible client (VS Code + Continue, Cursor, Kiro, a future
Telegram bot, a future dashboard) can point at. Under the hood it routes
each request to the appropriate backend — OpenRouter for cloud hard
turns (with free-tier models available by default), Ollama on the laptop
for everyday coding turns, Ollama on the desktop as a local fallback —
based on the model alias the client asked for. Direct Anthropic access
is supported in config but disabled by default; enable it if OpenRouter
ever proves insufficient. The gateway runs as a Windows service,
survives reboots, and logs every request with an estimated cost so the
user can see spend without a dedicated database or cap subsystem.

Phase 1's job is to make "talk to FITT" mean "talk to one URL from
anywhere on Tailscale, get the right model behind it." Everything else
— memory, sessions, tools, Telegram — builds on this.

## User Stories

### 1. Unified chat endpoint

As a user, I want a single URL I can point any OpenAI-compatible client
at, so that all my AI interactions flow through one place regardless of
which backend ends up serving them.

#### Acceptance Criteria
- 1.1 The gateway exposes `POST /v1/chat/completions` on the desktop,
  reachable from any device on my Tailscale network.
- 1.2 Requests follow the OpenAI chat-completion schema (messages array,
  model, stream, temperature, max_tokens, etc.).
- 1.3 Streaming responses (`stream=true`) are forwarded to the client as
  Server-Sent Events in OpenAI format.
- 1.4 Non-streaming responses return the standard OpenAI JSON shape.
- 1.5 The endpoint is not reachable from outside Tailscale — verified with
  `netstat` showing bindings to localhost + Tailscale interface only, and
  with an external port scan showing 8080 closed.

### 2. Multi-backend model routing via aliases

As a user, I want to select the model per-request via a stable logical
name so I can use cloud models for hard problems and local Qwen for
cheap ones, and swap the underlying model later with a config change.

#### Acceptance Criteria
- 2.1 Clients name *aliases* (`fitt-default`, `fitt-smart`, `fitt-fast`);
  the router resolves aliases to concrete models via `config.yaml`.
- 2.2 Passing a cloud-backed alias routes to the configured cloud
  provider (OpenRouter by default; Anthropic if explicitly enabled).
- 2.3 Passing an Ollama-backed alias routes to the configured endpoint
  (laptop or desktop).
- 2.4 Unknown aliases return HTTP 400 with a body listing the available
  aliases.
- 2.5 If the primary backend for an alias is unreachable, the gateway
  falls back to the model's configured `fallback` id and the response
  includes an `X-FITT-Backend` header naming the actual backend used.
- 2.6 No client ever hardcodes a concrete model id like
  `qwen2.5-coder:14b`. Only aliases are valid in the `model` field.

### 3. Authentication

As the owner, I want the gateway to reject requests that aren't mine so
that someone else on my Tailscale network — or a process on my own
machines I didn't intend to grant access to — can't freely spend my
cloud-provider credits.

#### Acceptance Criteria
- 3.1 All `/v1/*` endpoints require an `Authorization: Bearer <token>`
  header.
- 3.2 Valid tokens are configured in `~/.fitt/secrets.yaml`, never in
  environment variables committed to git or in `config.yaml`.
- 3.3 Invalid or missing tokens return HTTP 401 and no backend call is
  made.
- 3.4 Health endpoints (`/health`, `/ready`) do not require authentication.
- 3.5 Token comparison uses a constant-time comparison (e.g.
  `secrets.compare_digest`) to prevent timing-based extraction.
- 3.6 The gateway refuses to start if `secrets.yaml` is world-readable.

### 4. Always-on operation

As a user, I want the gateway to survive reboots and crashes so I don't
have to manually restart it.

#### Acceptance Criteria
- 4.1 The gateway runs as a Windows service (NSSM or `sc.exe`) on the
  iBUYPOWER desktop.
- 4.2 If the process crashes, it auto-restarts within 30 seconds.
- 4.3 After a full reboot, the gateway is reachable on the Tailscale IP
  within 60 seconds without any manual steps.

### 5. Observability

As a developer, I want to see what the gateway is doing so I can debug
routing and cost issues without standing up a dashboard.

#### Acceptance Criteria
- 5.1 Every request is logged with: timestamp, alias requested, resolved
  model, backend chosen, latency, token counts (input/output), and —
  when applicable — estimated cost in USD.
- 5.2 Logs go to a rotating file in `~/.fitt/logs/gateway.log` (daily
  rotation, 30-day retention).
- 5.3 Prompt and response bodies are not logged by default. A
  `log_bodies: true` flag in config enables debug-only full logging.
- 5.4 `GET /v1/models` returns the list of configured aliases and their
  current resolvability (is the primary backend reachable right now?).
- 5.5 `GET /health` returns 200 if the gateway process is alive.
- 5.6 `GET /ready` returns 200 only if at least one backend is reachable
  for every configured alias.
- 5.7 A `fitt cost` CLI command tails the log, aggregates
  month-to-date spend by model, and prints a summary. No separate
  database required.

### 6. Failure handling

As a user, I want the gateway to degrade gracefully when backends misbehave
so a transient upstream issue doesn't silently corrupt my session.

#### Acceptance Criteria
- 6.1 Upstream HTTP 429 (rate limited) or 529 (vendor overloaded, e.g.
  Claude) is surfaced to the client as HTTP 503 with a `Retry-After`
  header. The gateway does not auto-retry.
- 6.2 If the primary Ollama endpoint for an alias is unreachable, the
  gateway tries the configured `fallback` once, logs the failover, and
  sets `X-FITT-Backend` to the actual backend.
- 6.3 If no backend is reachable for the requested alias, the gateway
  returns HTTP 503 with a body naming the alias and the attempted
  backends.
- 6.4 Mid-stream upstream failures terminate the SSE stream cleanly with
  an `[ERROR]` event rather than silently truncating.

### 7. IDE integration

As a user, I want my laptop's VS Code (with Continue) to talk to the
gateway for all model requests so I have a single place to swap or
upgrade models.

#### Acceptance Criteria
- 7.1 Continue, configured with the gateway's Tailscale URL and a valid
  Bearer token, can chat using `fitt-default` and `fitt-smart` aliases.
- 7.2 Streaming responses appear token-by-token in the Continue chat
  panel.
- 7.3 End-to-end latency for a short cloud-backed request (via gateway)
  is within 500ms of hitting the same cloud backend directly from the
  same network.

## Non-Goals (explicit)

- **Cost-cap enforcement middleware.** Each cloud provider has its own
  per-key spend controls (OpenRouter in the dashboard, Anthropic in the
  console). Those are the source of truth. If ever insufficient, Phase
  10 revisits.
- **Multi-user / multi-tenant.** Single-user, single-home. One Bearer
  token is enough for v0. (The token list in `secrets.yaml` supports
  multiple tokens so future interfaces can have their own, but one
  suffices now.)
- **Memory or context injection.** That's Phase 2.
- **Tool use or MCP.** That's Phase 4.
- **HTTPS with real certificates.** Tailscale provides the network
  isolation and optional TLS via Tailscale Serve. HTTP over Tailscale is
  acceptable for v0.
- **A web dashboard.** Open WebUI arrives in Phase 3. Phase 1 is headless.
- **Telegram bot integration.** That's Phase 3. The Phase 1 secrets
  template *reserves space* for the Telegram bot token and allowlist so
  the user only has to touch `secrets.yaml` once, but Phase 1 code does
  not read those fields.

## Shareable-by-construction

This spec is written to be used by anyone, not just the original
author. Concretely:

- **No personal values in the repo.** All configs are `.example` files
  with placeholders. All machine-specific values (Tailscale IPs, tokens,
  API keys, paths) live in the user's `~/.fitt/` directory.
- **No hardcoded paths, names, or IPs in code.** Everything
  user-specific comes from `config.yaml` or `secrets.yaml`.
- **Setup takes minutes, not hours.** A new user clones the repo, copies
  `configs/*.example.yaml` to `~/.fitt/`, fills in their values, runs
  the install script. No code edits required to "make it about them."
