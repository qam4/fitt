# Phase 4.8 — Visibility Proxies: Requirements

## Context

The author can't see what FITT is doing without SSHing into the
NAS and reading six JSONL files. This wasn't urgent when FITT
was a toy; it is urgent now. The 2026-05-10 Telegram session
surfaced four overlapping visibility gaps (Problem D in
`docs/hallucinations-and-poisoning.md`):

1. **In-turn visibility.** The author saw a final reply but
   couldn't tell what the model deliberated over, what tools
   were called, what approvals pended. Everything the chat
   endpoint knows is thrown away at response time.
2. **Cross-turn visibility.** `fitt inbox`, `fitt audit tail`,
   `fitt capability-gaps` all exist but require `docker
   compose exec gateway fitt ...` on the NAS. The author
   doesn't have a shell on the Hub in their muscle memory;
   visibility-gated-on-shell is visibility-gated-on-never.
3. **Mobile visibility.** The phone is where the author spends
   most of their Telegram time. Today the phone sees only the
   final model reply. An approval prompt sits between messages
   as a UI artifact; no listing of past events, approvals,
   gaps.
4. **Third-party visibility.** Raycast widgets, shell scripts,
   dashboards, any future admin-UI — none can consume FITT
   state without learning the on-disk format.

The full admin dashboard (Phase 7+) is the long-term answer,
but it's 2-3 weekends of focused work that we aren't ready to
start. Phase 4.8 ships visibility proxies — small surfaces
that render the same data the dashboard will eventually render
— in order of phone/IDE reach. Each proxy is independently
useful and reusable when the dashboard lands.

**Principle 4 pairing:** each sub-phase leaves something
usable. Proxy #1 alone (per-turn event stream + `fitt watch`)
closes most of gap #1 and some of gap #2. Proxies #2-4 build on
it. Shipping piecemeal is explicit and deliberate.

**Principle 11 pairing:** the per-turn event stream makes
detectable misconfigurations visible at the lowest cost we
know how to produce — one JSONL line per interesting event,
rendered any way the operator wants.

## User stories

### U1. Per-turn event stream

As a FITT developer, I want a structured record of what
happened in each chat turn — LLM calls, tool invocations,
approvals, gap reports — so the failure modes we live
with stop being invisible.

**Acceptance:**

- **1.1** Every tool-using turn writes one JSONL file at
  `$FITT_HOME/sessions/<session>/turns.jsonl`. One line per
  event within the turn.
- **1.2** Event kinds include `turn_started`, `llm_call_started`,
  `llm_call_completed` (with model, latency, token counts,
  cost), `tool_call_planned` (name + args), `approval_requested`,
  `approval_decided` (or `approval_timed_out`),
  `tool_call_executed` (result summary, exit code for shell,
  duration), `gap_reported`, `turn_finished`. Schema pinned
  in the spec's design.md.
- **1.3** Events emit from the existing call sites in
  `agent_loop.py`, `chat.py`, `cron_runner.py`,
  `approval.py` — no new instrumentation layer; this is
  structuring signals we already emit as logs or events.
- **1.4** Emission is non-blocking. A full disk, an unwritable
  path, or any other IO failure logs a warning and the turn
  continues. Losing visibility is worse than losing FITT.
- **1.5** The existing `events.jsonl` (Phase 4.5) is
  unchanged. Per-turn is a sibling store, not a replacement —
  `turns.jsonl` is high-cardinality per-session detail;
  `events.jsonl` is per-hub user-visible activity. Different
  retention, different readers.
- **1.6** File layout follows the Phase 5 session-scoped
  convention. Retention piggybacks on the history pruner
  extended in the tool-artifact hoisting commit.

### U2. `fitt watch` CLI renderer

As a FITT developer, I want a live-tail CLI for the per-turn
stream so I can watch a session in flight from any shell
that can reach `$FITT_HOME`.

**Acceptance:**

- **2.1** `fitt watch <session>` tails the session's
  `turns.jsonl` with a concise, color-coded per-line format.
  Tool calls expand inline to show the arguments. Approvals
  show pending state until resolved.
- **2.2** `fitt watch --session-active` picks whatever session
  has the most recent turn (most common case — author just
  asked something in Telegram and wants to see what happened).
- **2.3** Renderer works with `docker compose exec gateway
  fitt watch ...` as well as with a local workspace pointing
  at a bind-mounted `$FITT_HOME`. Deployment-neutral per the
  project overview.
- **2.4** Output is Kiro-style concise: one line per event,
  never wraps into multi-line banners for common shapes.

### U3. Telegram `/inbox` command

As a FITT user with a phone in hand, I want a bot command
that shows recent events so I can see what FITT has been
doing without switching to a shell.

**Acceptance:**

- **3.1** A `/inbox` command in the bot returns the last N
  events (default 20) across sessions, paged via the bot's
  existing pagination helpers.
- **3.2** Same data as `fitt inbox` (cross-session). Events
  render with the Phase 4.5 formatters that already exist
  for the push channel; we're reusing, not reformatting.
- **3.3** `/inbox` respects per-client auth — only tokens
  tagged `telegram` can call it.
- **3.4** Filter flags: `/inbox cron`, `/inbox errors`,
  `/inbox session=<id>` narrow the view.

### U4. HTTP read endpoints

As a future dashboard builder (and present Raycast/widget
author), I want HTTP endpoints that return the same data the
CLI reads so clients stop having to know the on-disk format.

**Acceptance:**

- **4.1** `GET /v1/events?since=<ts>&kind=<k>&session=<s>` —
  paged events.jsonl reader with the same filters the
  `fitt inbox` CLI supports. JSON response.
- **4.2** `GET /v1/audit?since=<ts>` — audit log reader.
  Read-only; HMAC chain verification remains CLI-only.
- **4.3** `GET /v1/capability-gaps` — ranked gap log, same
  shape the `fitt capability-gaps` CLI produces.
- **4.4** `GET /v1/sessions/<id>/turns?since=<ts>` — per-turn
  event stream for one session.
- **4.5** Bearer auth via the existing token machinery. No
  per-endpoint ACL, no write endpoints. Read-only.
- **4.6** Cursor-based pagination where relevant (events,
  audit, turns). No total-count headers — the logs are
  append-only and the client can derive "am I caught up" by
  comparing to the log's tail.

### U5. Static HTML viewer

As an operator on a phone browser, I want a self-contained
HTML page that polls the events endpoint so I can watch events
land without a native client.

**Acceptance:**

- **5.1** `GET /v1/events/view` returns one HTML page with an
  inline `<script>` that HTMX-polls `GET /v1/events` every 5
  seconds and appends new rows to the top of a list.
- **5.2** No build step, no templates, no framework. Single
  file embedded in the gateway; lives as an adjacent Python
  string or file.
- **5.3** Bearer auth via a `?token=<token>` query parameter
  (HTTP-Basic would be fine too; we take the simpler path).
  Tokens stay the existing ones from `secrets.yaml`.
- **5.4** Explicit stepping-stone framing: when the real Phase
  7+ dashboard lands, this endpoint either stays as a
  fallback or redirects.

## Scope boundaries

- **No writes from HTTP or HTML.** Read-only surface in this
  phase. Writes still go through the chat endpoint.
- **No authentication beyond the bearer token that already
  exists.** No per-event ACLs, no OAuth, no user-scoped
  views. Single-operator deployment.
- **No log rotation for `turns.jsonl` in this phase.** Same
  posture as `events.jsonl` / `audit.jsonl` — append-only,
  history-pruner handles retention.
- **No admin dashboard.** The real editable surface over
  `$FITT_HOME` (config diffing, session browser with edit,
  live turn view with inline replay) is Phase 7+. This phase
  is the "can I see what happened in the last 10 minutes"
  floor.
- **No subagents, no parallel execution visibility.** FITT's
  agent runs turn-by-turn today; when Phase 7+ ships
  subagents, the stream design extends to cover multi-agent
  turns.

## Out-of-scope variants we deliberately rejected

- **Streaming events over SSE to the HTML viewer.** 5-second
  polling is plenty for a single-user surface; SSE adds
  complexity (reconnect logic, heartbeat, per-client state)
  that Phase 4.8's "ship fast, replace with the dashboard
  later" posture doesn't want.
- **Filtering the in-flight turn stream by tool type.** Not
  worth the config surface; the CLI does its own grep.
- **Markdown rendering of events in the HTML viewer.** Plain
  text with a monospace font is what you want when you're
  trying to read three tool-call-argument dicts fast.

## Prerequisites

- Phase 4 (events.jsonl exists).
- Phase 4.5 (event pushing, `fitt inbox`).
- Phase 4.7 (tool_executed event kind + stable shape for
  tool-call metadata).
- Boot-time alias probe (ditto for `alias_probe.*` log
  lines the stream-stage-one can mirror).

## Non-goals for the sub-phases

Sub-phase decomposition (see design.md):

- **4.8a**: backend + persistence + schema + emission sites.
- **4.8b**: `fitt watch` CLI.
- **4.8c**: HTTP read endpoints.
- **4.8d**: static HTML viewer.
- **4.8e**: Telegram `/inbox` command.

Each sub-phase lands independently and is usable on its own.
Order is 4.8a first (dependency for everything), then any
order for the rest.
