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

### U3. Telegram live-turn renderer

As a FITT user with a phone in hand, I want Telegram to show
me what FITT is doing as a turn unfolds, notify on blocking
approvals, and notify when the turn is done — so a 30-second
multi-step turn stops being a silent progress-less wait.

Design lifted from MeshClaw's Slack gateway after reading
its source 2026-05-13 (see
`src/mesh_claw/slack/handler.py::handle_message`). Three
bubble types per turn (fewer on short-chat turns). Telegram-
adapted because Telegram edits-don't-notify where Slack
thread activity does.

**Bubble shape per turn:**

1. **Growing stream bubble.** Single message per turn,
   silent edits throughout. Contains every tool-call status
   line accumulated as task-card-style entries ("🔵 Reading
   README.md…" → "✅ Read README.md (12ms)"), plus model
   narration text streamed in as the model produces it, plus
   the final reply text streamed in via the existing chat
   streaming path. Never recreated, never replaced — a
   growing chronological record of what happened, posted at
   turn-start time.
2. **Approval bubbles (zero or more).** Each
   `approval_requested` posts a new notifying message with
   the inline ✅ / ❌ / 🔓 keyboard. On decision, the
   message edits in place to show the outcome ("🔐 edit_file
   → ✅ Approved", "→ ❌ Rejected", "→ ✅ Approved for
   session"). The message stays at its posted timestamp in
   the timeline forever. Phone buzzes exactly once per
   approval — the blocking moment you need to know about.
3. **Finish footer.** One notifying message posted at
   turn-finished time. Minimal content — "✓ Finished in 9s"
   or "🚫 Rejected" for the turn-cancelled case. Phone
   buzzes exactly once, signaling the turn is done. The
   final reply text itself lives in the growing stream
   bubble, not in the footer; the footer is a ping-the-phone
   mechanism, not where the answer lives.

**Acceptance:**

- **3.1** Each `tool_call_planned` / `tool_call_executed`
  pair renders as one task-card line in the growing bubble.
  Planned = "🔵 Reading X…"; executed = "✅ Read X
  (Nms)" (success) or "❌ Read X — error summary" (failure).
  Edits are silent (no phone notification).
- **3.2** Each `approval_requested` posts a new notifying
  bubble with the approval UI. On decision the buttons
  clear and the message edits to the outcome. Phone
  notifies on post; no further notifications after edits.
- **3.3** `turn_finished` posts a tiny notifying footer
  message. Short-form timing: "✓ Finished in Ns", or
  "🚫 Rejected" for the turn-cancelled case. Phone
  notifies once. The final reply text itself lives in the
  growing bubble (streamed in by the existing chat
  streaming path).
- **3.4** Short chat turns (zero tool calls, zero
  approvals) skip the growing bubble machinery entirely —
  the existing chat streaming behaviour (the model reply
  as a single message posted as a new bubble) stays
  unchanged. No finish footer posted either; the reply
  message IS the turn-finished notification. Preserves
  today's behaviour for casual replies like "thanks" /
  "you're welcome" so scrollback stays quiet.
- **3.5** Errors stay visible. A failed tool call locks at
  its ❌ form in the growing bubble with a short error
  snippet. The turn continues; later tool calls append
  after it.
- **3.6** Timeline ordering is correct by construction.
  The growing bubble is at turn-start timestamp. Approval
  bubbles are at their individual post timestamps. Finish
  footer is at turn-finished timestamp. Telegram orders by
  send time, so scrollback reads top-to-bottom in the
  actual chronology. The 2026-05-12 "approval floats
  between messages after decision" bug goes away because
  no bubble has a send time earlier than its related
  content.
- **3.7** No in-bubble "waiting for approval" breadcrumb
  inside the growing stream. MeshClaw considered and
  rejected this in favour of scrollback cleanliness; the
  approval bubble itself is the record of what was
  approved. Revisit only if scrollback interleave turns
  out to be an actual daily confusion.
- **3.8** The bot subscribes to the per-turn event stream
  (U1) via the SSE endpoint in 4.8c. The JSONL
  persistence is the source of truth; the subscriber is a
  stateless formatter over live events.

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

*Deferred to Phase 7+ as part of the real admin dashboard.*
Originally scoped as a read-only barebone dashboard
(`GET /v1/events/view` returning a single self-contained
HTML page with HTMX auto-refresh). Decided 2026-05-13 to
not ship a stepping-stone dashboard — the real dashboard
in Phase 7+ will cover the same phone-browser surface
with config editing and session browsing that the
barebone version can't touch, and the daily phone
experience is Telegram (U3), not a browser tab. Keeping
the one-URL viewer would have been a half-day of work but
would have meant maintaining a "mini-dashboard" in the
same tree as the "real dashboard" once Phase 7+ ships.
Dropping v0 avoids that split.

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
- **Growing-bubble Telegram renderer (single message per
  turn, status lines accumulate, recreate-on-approval to
  trigger notification).** Considered and rejected in favour
  of one-message-per-action. Growing-bubble's scrollback-
  density benefit doesn't hold up under scrutiny — the same
  content is the same content regardless of wrapper. One-
  per-action is simpler to implement, has correct timeline
  ordering by construction (no recreate-and-resend), and
  gives each bubble a single clear role. If real use later
  shows one-per-action produces too many bubbles to read,
  revisit.
- **`/inbox` historical browser** (bot-side paged view of
  `events.jsonl`). Deferred to post-v1 because the live
  renderer (U3) covers the "see what just happened" case
  that matters more. An operator who wants to scroll past
  turns from their phone can re-request with a specific
  session or time range via a future `/inbox` follow-up.

## Prerequisites

- Phase 4 (events.jsonl exists).
- Phase 4.5 (event pushing, `fitt inbox`).
- Phase 4.7 (tool_executed event kind + stable shape for
  tool-call metadata).
- Boot-time alias probe (ditto for `alias_probe.*` log
  lines the stream-stage-one can mirror).

## Non-goals for the sub-phases

Sub-phase decomposition (see design.md):

- **4.8a**: backend + persistence + schema + emission sites
  + in-process pub/sub hook.
- **4.8c**: HTTP read endpoints + SSE stream for turns.
  Promoted before 4.8b because the bot consumes the stream
  over HTTP, not in-process.
- **4.8b**: Telegram live-turn renderer (subscribes to the
  SSE endpoint).
- **4.8d**: `fitt watch` CLI (developer / debugging tool).

HTML viewer (former 4.8e) deferred to Phase 7+ as part of
the real admin dashboard.

Order is 4.8a first (dependency for everything), then 4.8c,
then 4.8b, then 4.8d. Each sub-phase is still independently
useful and testable.
