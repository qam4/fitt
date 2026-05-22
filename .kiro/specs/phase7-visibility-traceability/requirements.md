# Phase 7 — Visibility & Traceability: Requirements

## Context

The 2026-05-22 granite-narration debugging session crystallised
what's been a slow burn since Phase 4 shipped. Granite 3.3 8B
was bound to `fitt-default`. Telegram users got narrated JSON
in `message.content` instead of structured `tool_calls`. To
diagnose, we read source for `chat.py`, `agent_loop.py`,
`router.py`, `capabilities.py`, and `alias_probe.py`; ssh'd
into the hub; ran curl directly against Ollama; ran curl
through the gateway with `coding-agent` mode to bypass
prompt-injection; compared token counts (159 bare vs 5400
with FITT's system prompt) to discover the system-prompt
size was the load-bearing variable. None of that is the
daily-debugging path a single-user system can sustain.

Two distinct gaps surfaced:

1. **Live visibility — "what is FITT doing right now."**
   Phase 4.8 already ships some of this (per-turn event
   stream, Telegram live-turn renderer, `fitt watch` CLI,
   SSE endpoint). What's missing is the per-binding metadata
   that turns "another tool call ran" into "another tool
   call ran on granite3.3:8b at 23% of its 32k context
   window." Without per-binding context awareness the live
   stream tells you what happened mechanically; it doesn't
   tell you whether the binding is healthy.

2. **Traceability — "what did FITT do, and why."** Given a
   surprising result ("this Telegram reply looked weird"),
   the operator should be able to walk back to the exact
   dispatched body, exact response, exact tool calls,
   exact context size — within seconds. Today the route to
   that detail is multi-step: read `events.jsonl`, cross-
   reference `audit.jsonl`, cross-reference `turns/<date>.jsonl`,
   reconstruct the system prompt from memory, guess at the
   capability block. Phase 4.8 captures live signals; this
   phase persists enough per-turn detail to reconstruct any
   past turn fully.

These two gaps share a data backbone (per-turn capture) and
two operator surfaces (Telegram, dashboard). Splitting the
phase into independent slices is a deliberate scoping choice
— each slice is independently shippable.

**Principle 11 pairing.** "Fail loud on detectable
misconfigurations" generalises here to "expose detectable
misconfigurations to the operator surface, not just the log
file." Per-binding context windows, prompt-size warnings, and
narration shape signals are all detectable; today they live in
logs only.

**Principle 8 pairing.** "The agent is honest about its
capabilities" extends to "the system is honest about what it's
doing." Traceability is principle 8 applied to operator
debugging, not just to model self-report.

**Principle 9 pairing.** "Live with it before extending it"
applies to phase-internal pacing too. Each slice ships,
gets used, informs the next. Slice 7.1 (context awareness) is
the foundation but only earns its keep when 7.3 (`/model`)
and 7.5 (dashboard) read from it. Ship 7.1 with a minimal
exposure surface (the new `/v1/aliases` endpoint), use it,
then build the surfaces.

## User stories

### U1. Per-binding context awareness

As a FITT operator binding new models, I want the gateway to
discover and surface each backend's effective context window
so I can tell "the prompt is at 23% of the limit" from "the
prompt is at 92% of the limit" without ssh'ing into the
backend.

**Acceptance:**

- **1.1** For every alias in the configured chain, the gateway
  resolves the effective context window of the bound model at
  startup. Per backend:
  - **Ollama**: query `POST /api/show` for the model. Prefer
    the modelfile's `num_ctx` parameter when set; fall back
    to the architecture's natural ceiling reported in
    `model_info`; finally fall back to Ollama's documented
    default (2048) when neither is parseable.
  - **OpenAI-compatible** (OpenRouter, NVIDIA NIM, Groq,
    Together, generic openai-shape endpoints): query
    `/v1/models` and read the `context_length` field of the
    matched model entry.
  - **Anthropic**: ship a small lookup table keyed on model
    family (Sonnet 4.5: 200k; Opus 4: 200k; Haiku family:
    200k unless otherwise known). Anthropic doesn't expose
    a discovery endpoint that returns context length.
- **1.2** Discovery is best-effort and never blocks gateway
  start. A discovery failure logs an ERROR with the
  alias / backend / failure reason (Principle 11 shape) and
  records `context_window: null` for that binding. The
  binding still serves requests; downstream consumers
  (Slice 7.3 `/model`, dashboard) display "context window
  unknown" rather than guessing.
- **1.3** Results cache for the lifetime of the gateway
  process (rebooting refreshes them; matches the
  config-reload-via-restart posture). `fitt context refresh`
  CLI exists for the case where the operator wants to
  re-probe without a restart (e.g. they just changed Ollama's
  `OLLAMA_CONTEXT_LENGTH` env var).
- **1.4** A new endpoint `GET /v1/aliases` returns the
  per-alias detail: alias id, primary backend, primary
  model, fallback model id (if any), discovered context
  window, last probe result with timestamp, last eval
  pass-rate with timestamp. Bearer auth, read-only. Shape
  pinned in design.md.
- **1.5** The existing `GET /v1/models` endpoint stays
  unchanged for backward compatibility (clients depend on
  the OpenAI-shape response). `/v1/aliases` is the
  FITT-shaped richer view.
- **1.6** `MemoryStore` and `chat.py` gain optional access
  to the discovered context window so future
  prompt-budget warnings (deferred to Slice 7.3) and
  Phase 8's compaction trigger have a real ceiling rather
  than a guess.

### U2. Per-turn traceability capture

As a FITT operator chasing a surprising result, I want every
turn's full detail (dispatched body, response, tool calls,
context fill) captured in a way I can reconstruct after the
fact, so "this reply looked weird" becomes a 30-second
lookup rather than a multi-source reconstruction.

**Acceptance:**

- **2.1** Phase 4.8's `TurnLog` already captures lifecycle
  events (`turn_started`, `llm_call_*`, `tool_call_*`,
  `approval_*`, `gap_reported`, `turn_finished`). This
  story extends the per-turn record with bodies: the
  dispatched message list, the upstream response object,
  the structured tool-call chain.
- **2.2** Storage shape: a sidecar JSON file per turn at
  `$FITT_HOME/sessions/<session>/turns/<YYYY-MM-DD>/<turn_id>.json`
  next to the existing `<YYYY-MM-DD>.jsonl` event log. One
  file per turn, written once at turn-finished time. Schema
  pinned in design.md.
- **2.3** Captured fields per turn: `turn_id`, `session_key`,
  `alias`, `client`, `model_used` (concrete model id and
  backend), `fallback_used`, `started_at`, `finished_at`,
  `dispatched_messages` (full list as sent to LiteLLM),
  `response` (the final upstream response that produced
  the assistant text), `tool_calls` (structured chain of
  every tool call attempted in this turn — name, args,
  approval decision, result summary), `prompt_tokens`,
  `completion_tokens`, `context_window` (from Slice 7.1),
  `prompt_pct_of_window`, `finish_reason`,
  `narration_warning_flag` (boolean — set when finish
  reason is `stop` and content looks like a narrated
  tool call per the existing `is_tool_use_expected_but_none`
  classifier on the eval-harness side, but in chat we set
  it at turn-finished time only as an observability hint,
  never as a runtime gate; see design.md for why this
  isn't a return-of-the-rolled-back claim-check).
- **2.4** Privacy posture: capture is on by default for
  agent-mode clients (`telegram`, `webui`, `cli`, `ide`
  in non-router-mode, cron firings) and off by default
  for router-mode clients (`coding-agent`). Rationale:
  router-mode clients (Aider, Claude Code, Continue
  agent mode) often paste tokens, secrets, or proprietary
  code in their request bodies; capturing those would
  violate the "FITT is a thin router for coding agents"
  contract that Phase 4 established. Operators can flip
  the default via `traceability.default_capture` config.
- **2.5** Capture is non-blocking. An IO failure (full
  disk, unwritable path, race) logs a warning and the
  turn continues. Losing traceability is worse than
  losing FITT. Same posture as Phase 4.8's per-turn event
  emission.
- **2.6** Retention follows the existing
  `memory.history_max_days` setting. The history pruner
  extends to sweep `turns/<YYYY-MM-DD>/` directories on
  the same window.
- **2.7** A new endpoint `GET /v1/sessions/<session>/captures/<turn_id>`
  returns the captured detail as JSON. Bearer auth.
  Read-only. Returns 404 with a clear message when the
  turn id doesn't exist or capture was disabled for that
  client.
- **2.8** A `fitt turn show <turn_id>` CLI command prints
  the captured detail in a human-readable form. Wraps the
  HTTP endpoint same as `fitt watch` wraps the SSE stream.
- **2.9** Turn ids are deterministic and stable. The
  existing `turn_id` from Phase 4.8 (UUID generated in
  `chat.py`) is the lookup key. The chat handler already
  surfaces it in the `X-FITT-Turn-Id` response header.

### U3. Telegram operator commands

As a FITT user with a phone in hand, I want commands to
inspect FITT's state — what model answered, what the last
turn cost, what's healthy, run an eval — so the question "is
FITT okay?" doesn't require a desk and an ssh session.

**Acceptance:**

- **3.1** `/model` (extends today's behaviour, partial
  shipped 2026-05-22): lists every alias with its concrete
  model and backend. Marks the current alias for the
  chat. Surfaces context window when known. Shows last
  probe result and last eval pass-rate per alias.
  Switches alias when called with one argument; confirms
  with the new alias's concrete model.
- **3.2** `/lastturn`: prints a per-turn summary for the
  most recent turn in this Telegram chat. Includes alias
  requested, model used, prompt tokens, completion tokens,
  prompt as percent of context window, latency, finish
  reason, fallback flag, narration warning if any, tool
  calls run with their outcomes. Generated server-side
  from the captured turn JSON (Slice 7.2). When capture
  was off for the turn (router-mode), says so.
- **3.3** `/status` (or `/health`, name TBD in design.md):
  system-level health summary. MCP server statuses,
  cron jobs (count, next fire time, last fire outcome),
  history-pruner last sweep time, event-pruner last
  sweep time, capability-gap log size, gateway uptime,
  Tailscale-presence sanity check.
- **3.4** `/eval <alias>`: kicks the existing
  `alias_eval` harness for the alias and posts a
  short summary inline (pass rate, failed cases,
  link/path to the full report). Long-running call
  (~30-60s); the bot replies "running…" and edits in
  the result, same pattern as approval bubbles.
- **3.5** `/help` updated to list the new commands.
- **3.6** Each command honours the existing allowlist —
  unauthorised users get the same silent ignore as on
  text messages.

### U4. Telegram markdown renderer

As a FITT user reading model replies on a phone, I want
markdown to render correctly so `**bold**` arrives bold
instead of with literal asterisks.

**Acceptance:**

- **4.1** Bot-side conversion: CommonMark → Telegram HTML
  via `markdown-it-py`. Whitelist-sanitised to Telegram's
  allowed tag set: `<b>`, `<i>`, `<code>`, `<pre>`, `<a>`,
  `<blockquote>`, `<tg-spoiler>`. Unsupported tags (h1-h6,
  tables, lists with markers) degrade gracefully — strip
  the wrapper, keep the text content.
- **4.2** Applied at every Telegram-side message construction
  point that today emits raw markdown: streaming `_flush`
  in `streaming.py`, the event-push formatter for
  `send_message` events, the live-turn renderer's stream-
  bubble flush, approval prompt bodies, command responses
  that include model output (`/model` switch confirmation
  etc).
- **4.3** HTML chosen over MarkdownV2 because a half-written
  `<b>` degrades gracefully under streaming edits while a
  half-written `*…*` crashes the MarkdownV2 parser for the
  whole message. Trades MarkdownV2's "closer to source
  format" for HTML's "doesn't blow up mid-stream."
- **4.4** Code blocks (` ``` ` fenced) preserve their
  monospace rendering via `<pre>`. Inline code (`code`
  with single backticks) renders via `<code>`.
- **4.5** Links (`[text](url)`) render via `<a href="...">`,
  with URL passed through verbatim.
- **4.6** A regression test pins the 2026-05-22 user
  complaint: "model replies render `**bold**` literally"
  fails today; passes after the renderer ships.
- **4.7** The markdown renderer is independent of every
  other slice. Could land first or last; no data
  dependency. Listed under U4 because it's a Phase 7 ship,
  not because it's after U3 in dependency order.

### U5. Dashboard v0

As a FITT operator at a desk, I want a web dashboard
covering the same data Telegram surfaces, so longer
debugging sessions don't need ssh-into-container plus
six-tab grep workflows.

**Acceptance:**

- **5.1** A FastAPI route `/dashboard` mounted on the
  existing gateway. HTMX + server-rendered Jinja
  templates; no SPA build step. Bearer auth via the
  existing token machinery, or session cookie issued
  from a small login page that takes the bearer token
  once and stashes it in a signed cookie.
- **5.2** Tailscale-only by default — same posture as
  the chat endpoint. Operators on a public-internet
  exposure are responsible for their own reverse proxy.
- **5.3** Six core views, each on its own route:
  - `/dashboard/aliases` — per-binding state. Reads
    `/v1/aliases` (Slice 7.1). Each row shows alias,
    bound model, backend, context window, last probe
    result, last eval pass rate, and "recent
    dispatches" (count + average prompt size + any
    narration warnings) for the last 24 hours.
  - `/dashboard/turns` — per-session turn browser. Reads
    `/v1/sessions/<session>/captures/<turn_id>` (Slice 7.2).
    The centerpiece for traceability — one click on a
    turn row expands the full chain: dispatched system
    + history + user, response, tool calls, finish
    reason, prompt fill percent, narration warning flag.
    The same data the `/lastturn` Telegram command
    renders, in a browsable form.
  - `/dashboard/tools` — registered tools. Per-tool:
    name, description, default bucket per client, last
    invocation time, last failure, count over last 24h.
  - `/dashboard/cron` — scheduled jobs. Reads
    `cron.json` and recent firing events from
    `events.jsonl`. Shows next-fire and last-outcome.
    Read-only; no add/edit/delete in v0.
  - `/dashboard/audit` — filtered tail of `audit.jsonl`.
    Same filters `fitt audit tail` supports.
  - `/dashboard/health` — system status (the same data
    `/status` Telegram command renders).
- **5.4** Plus two supporting views:
  - `/dashboard/gaps` — capability-gap log, ranked.
    Same data as `fitt capability-gaps` CLI.
  - `/dashboard/overview` — landing page. "Is FITT okay
    right now?" snapshot. Per-alias one-line status,
    recent failure count, MCP server up/down,
    gateway uptime. Links to the detail views.
- **5.5** Live updates where they earn their keep.
  `/dashboard/turns` for an active session SSE-subscribes
  to the existing `/v1/sessions/<session>/turns/stream`
  endpoint (Phase 4.8c) and edits-in-place — the same
  data substrate the Telegram renderer consumes. No new
  rendering layer; reuse the existing event shapes.
  Other views poll on a 5-30s cadence; live-edit isn't
  necessary for cron, audit, or gaps.
- **5.6** No edit support in v0. Read-only. Editing
  `config.yaml`, `secrets.yaml`, `projects.yaml`,
  `cron.json`, identity files, lessons through the UI is
  a follow-up — real work (validation, atomic application,
  hot reload, error recovery) and not the load-bearing
  90% of "operator debugs FITT."
- **5.7** No "send a chat message" surface in v0. The
  dashboard is explicitly an operator pane, not a third
  chat client. Telegram and Open WebUI cover that
  surface; resist bloat.
- **5.8** No multi-user auth. Single-operator posture; the
  dashboard trusts whoever's on the tailnet with a
  bearer token. Per-user authorization is a follow-up
  for the day a second person wants access.

## Scope boundaries

- **No realistic-prompt eval mode in this phase.** The
  granite incident's natural follow-up is
  `fitt eval alias <name> --realistic` — extend the
  eval harness to construct the system prompt the way
  live chat does. That's listed in Phase 12+ opportunistic.
  Phase 7 surfaces the data (per-turn capture in Slice 7.2)
  that makes the realistic-eval the obvious next step;
  the implementation itself is a separate ship.
- **No live config reload.** Today's restart-to-pick-up-config
  posture is fine for Phase 7. Hot reload involves
  validation, atomic application, error recovery, and is
  its own can of worms. The dashboard's read-only posture
  in v0 sidesteps the question entirely.
- **No compaction.** Phase 8 builds on the context-window
  discovery from Slice 7.1. Compaction is a separate
  phase. Phase 7 may show "prompt is at 92% of context
  window" warnings but does not act on them.
- **No vector memory.** Phase 9. Phase 7 surfaces what's
  already in `events.jsonl` / `audit.jsonl` /
  `turns/<date>.jsonl` plus the new per-turn capture; it
  doesn't add semantic recall.
- **No write surface in dashboard v0.** Read-only.
- **No SPA framework.** HTMX over server-rendered
  templates. The complexity budget says no Vue, no React,
  no build step in the hub container.
- **No new chat surface.** The dashboard does not send
  chat messages.

## Out-of-scope variants we deliberately rejected

- **Vue / React SPA dashboard.** Considered for live
  panels. Rejected: HTMX + SSE covers the live-update
  case for the centerpiece (Slice 7.5's turn view) and
  every other view is happy with 5-30s polling. SPA
  adds a build step, a frontend test harness, and a
  surface area we haven't budgeted maintenance for. If
  Phase 7 ships and someone insists on a richer
  interaction model, escalate to a follow-up.
- **Embedded TUI dashboard (Hermes pattern).** Hermes
  embeds their Ink TUI into the web UI via xterm.js +
  WebSocket-PTY. Clever; requires POSIX PTY, breaks on
  Windows hubs unless WSL2 is present. FITT's two-
  machine cluster has Linux on Compute and Windows on
  Hub; this would not work on the Hub. Rejected for
  the deployment-neutrality cost.
- **Editable dashboard in v0.** The right scope but not
  the right phase. Editing config and secrets through
  the UI is a real-work follow-up (validation, atomic
  apply, rollback, secret redaction). Read-only first;
  let the substrate prove itself; then add edit.
- **Markdown renderer via MarkdownV2.** Considered.
  Rejected — half-written `*…*` mid-stream-edit crashes
  the MarkdownV2 parser for the whole message.
- **Send chat from the dashboard.** Considered in
  brainstorming; rejected to keep the dashboard's
  identity as an operator pane, not a third chat
  surface.
- **Per-turn body capture for router-mode clients by
  default.** Considered: capture everything, treat
  privacy as the operator's problem. Rejected:
  router-mode clients (coding-agent) routinely paste
  tokens, secrets, and proprietary code; the
  thin-router contract is what makes router-mode
  tenable. Default capture off; operator can opt in
  per the config knob in 2.4.

## Prerequisites

- Phase 4 (event log, audit log, capability-gap log,
  tool registry, project registry).
- Phase 4.5 (events.jsonl persistence, `fitt inbox`,
  cron infrastructure).
- Phase 4.7 (project_shell tool, tool-call event shape).
- Phase 4.8 (per-turn event stream backbone, SSE
  endpoint, Telegram live-turn renderer, `fitt watch`
  CLI). Slice 7.5's dashboard reuses 4.8c's HTTP
  endpoints rather than building parallel ones.
- Phase 4.9 (upstream timeout discipline — error
  classification reuses the typed shapes the dashboard
  surfaces).

## Sub-phases

Slices map to commits and ship in any order they make
sense locally; only ordering constraint is data-flow:

- **Slice 7.1 — Context awareness.** Foundation. Adds
  `gateway/context_window.py`, the discovery cache, the
  `fitt context refresh` CLI, the `/v1/aliases` endpoint.
  Half day to a day.
- **Slice 7.2 — Per-turn traceability capture.** Builds
  on 7.1 (uses context_window in captured records).
  Adds the sidecar JSON store, the `/v1/sessions/.../captures/<id>`
  endpoint, the `fitt turn show` CLI, history-pruner
  extension. 1-2 days.
- **Slice 7.3 — Telegram operator commands.** Builds
  on 7.1 (`/model` reads context window) and 7.2
  (`/lastturn` reads captured detail). 1-2 days for
  `/lastturn`, `/status`, `/eval`; `/model` enrichment
  partial-shipped 2026-05-22.
- **Slice 7.4 — Telegram markdown renderer.**
  Independent. Could land first if someone has half a
  day; could land last. No data dependency.
- **Slice 7.5 — Dashboard v0.** Builds on every other
  slice. 2-3 weekends — bulk of the phase's calendar
  time.

## Definition of done

- All acceptance criteria above met or explicitly
  deferred to a follow-up (with cross-reference to the
  follow-up's home).
- `uv run pytest -q` passes in `gateway/` and
  `telegram-bot/`.
- `uv run mypy src` clean in both packages.
- `uv run ruff format src tests` and
  `uv run ruff check src tests --fix` clean in both.
- The granite-style debugging session can be
  reconstructed from the Telegram `/lastturn` command
  alone for any past turn (Slice 7.2 + 7.3) — the
  acceptance criterion is "I'd hit this case again,
  open Telegram, type `/lastturn`, and see the
  prompt-token + context-window numbers that took us
  two hours of curl-comparison to find."
- Dashboard v0 deployable via the existing docker compose
  flow without manual extra steps.
- Two-week live use per Principle 9 doesn't surface
  visibility gaps that should have been in scope.
