# Phase 4.8 — Visibility Proxies: Tasks

Five sub-phases. 4.8a is the dependency for everything else,
and 4.8c (HTTP endpoints including turns SSE stream) is a
dependency for 4.8b (Telegram renderer). The bot consumes the
turn stream over SSE rather than importing the gateway
in-process so today's split-container compose topology keeps
working. The in-process ``TurnLog.subscribe`` hook is what the
SSE handler uses to fan events out; it's not bypassed.

## Sub-phase 4.8a — Per-turn event stream backend + pub/sub

- [x] Create `gateway/src/gateway/turns.py` with
      `TurnEvent` dataclass, `TurnLog` class (append, read,
      file_path, new_event). Per-session-per-day layout at
      `sessions/<k>/turns/<YYYY-MM-DD>.jsonl`.
      *Shipped commit `0d974c8`.*
- [x] Pin the event-kind schemas (`TURN_EVENT_KINDS`).
      *Shipped in `turns.py`.*
- [x] Add `TurnLog.subscribe(callback)` and fire registered
      callbacks after each successful append. Raising
      callbacks get logged, never break persistence.
      *Shipped commit `bb05a2d`.*
- [x] Turn-event emission helpers in
      `gateway/src/gateway/turn_events.py` (record_*
      wrappers per event kind). *Shipped commit `30f182a`.*
- [x] Add `turn_id: str | None = None` to `ToolContext`.
- [x] Wire `record_turn_started` / `record_turn_finished` in
      `chat.py` around the tool-loop entry.
- [x] Wire `record_llm_call_started` / `record_llm_call_completed`
      in `agent_loop.py` around the dispatch call.
- [x] Wire `record_tool_call_planned` in `execute_tool_call`
      at entry; `record_tool_call_executed` at return.
- [x] Wire `record_approval_requested` in
      `ApprovalMiddleware.request_approval`.
- [x] Wire `record_approval_decided` in
      `resolve_approval` + timeout branch of
      `_request_and_wait`.
- [ ] Extend the existing `record_gap` in `agent_loop.py` to
      also emit turn events via `record_gap_event`.
- [x] Generate `turn_id` once per chat request in `chat.py`
      and on every cron firing in `cron_runner.py`; pass to
      the tool context.
- [x] TurnLog lifecycle in `app.py`: construct at boot,
      attach to `app.state.turns`, inject into ToolContext
      at request/fire time.
- [x] Hook the history pruner to walk
      `turns/<YYYY-MM-DD>.jsonl` via the same date-parsing
      code that already sweeps history and artifact dirs.
- [x] Integration test: full agent-loop turn with stubbed
      LLM produces the expected event sequence in both the
      JSONL file AND the subscriber callback list.

**Exit criteria:** a tool-using turn writes a valid
`turns/<date>.jsonl` with all the expected event kinds in
order; a registered in-process subscriber observes the same
events in the same order. `ruff` / `mypy` / `pytest` clean.

## Sub-phase 4.8c — HTTP read endpoints (promoted ahead of 4.8b)

Promoted from v1's "sub-phase 4.8d" to run before the
Telegram renderer because 4.8b depends on the SSE streaming
endpoint landing first. No tech-debt "bot-imports-gateway-
in-process" shortcut — the bot talks HTTP like any other
consumer.

- [ ] Extend (or create) `gateway/src/gateway/events_endpoint.py`
      / rename to a broader name. Add routes for:
      - `GET /v1/events`
      - `GET /v1/audit`
      - `GET /v1/capability-gaps`
      - `GET /v1/sessions/{id}/turns` (paged JSON read)
      - `GET /v1/sessions/{id}/turns/stream` (SSE live)
- [ ] Pagination via `since=<ts>` / `limit=<n>`. Response
      shape `{entries, next_since}`.
- [ ] SSE endpoint: on connect, optionally replay since a
      caller-supplied `since`; then stream new events via
      a `TurnLog` subscriber (in-process fanout to the HTTP
      handler's queue). Heartbeat every 15s to keep the
      connection alive and detect dead clients.
- [ ] Auth via existing `AuthMiddleware`; no per-endpoint
      ACL in this phase.
- [ ] `TestClient` tests: happy path, 401 on bad token,
      `since` filtering correct, `limit` bounded,
      `next_since=null` at tail, SSE delivers appended
      events, SSE replays from `since` on connect, SSE
      disconnects cleanly when the client hangs up.
- [ ] Per-session filter for `/v1/sessions/{id}/turns`
      returns only that session's events.
- [ ] Document the endpoints in `README.md` or a new
      `docs/http-api.md`.

**Exit criteria:** a curl against each endpoint returns
well-formed JSON. A curl against the SSE endpoint stays
connected and receives new events as they're appended.
Tests clean.

## Sub-phase 4.8b — Telegram live-turn renderer

Subscribes to the gateway over SSE (4.8c's stream endpoint).
Maintains per-turn UI state: one growing stream bubble per
turn (silent edits), notifying approval bubbles (edit-in-
place to outcome), plus a tiny notifying finish footer.
Depends on 4.8c landing first.

- [ ] Create
      `telegram-bot/src/fitt_telegram_bot/turn_renderer.py`
      with `TurnRenderState` dataclass and the event-to-
      Telegram-action state machine per design.md § "Telegram
      live-turn renderer".
- [ ] SSE subscriber in the bot: long-lived
      `httpx.AsyncClient.stream` connection to
      `GET /v1/sessions/*/turns/stream` with
      reconnect-with-backoff on disconnect.
- [ ] `tool_call_planned` → append "🔵 …" line to the
      growing stream bubble's text; lazy-post the bubble on
      the first append, silent notification flag.
- [ ] `tool_call_executed` → find the matching in-flight
      line in the stream bubble's text and rewrite it to
      "✅ Read X (Nms)" (success) or "❌ Read X — error"
      (failure). Silent edit.
- [ ] `llm_call_completed` / narration-token events → append
      the narration to the stream bubble, rate-limited at
      ~1 edit per second.
- [ ] `approval_requested` → post a NEW notifying message
      with ✅ / ❌ / 🔓 inline keyboard; record
      `approval_bubbles[approval_id]`.
- [ ] `approval_decided` → edit the approval message in
      place to the outcome ("🔐 edit_file → ✅ Approved"
      etc.); buttons clear.
- [ ] `turn_finished` → post a NEW notifying finish footer
      message ("✓ Finished in Ns" / "🚫 Rejected" / "⏱️
      Timed out"); drop the `TurnRenderState`.
- [ ] Short-chat turn detection: if no `tool_call_planned`
      or `approval_requested` fires between `turn_started`
      and `turn_finished`, the stream bubble and finish
      footer are both skipped — the existing chat streaming
      path posts the reply as today's single message. No
      scrollback pollution for "thanks" / "you're welcome".
- [ ] Rate-limit coalescing for stream bubble edits: track
      `last_stream_edit_ts`; events within 1s buffer and
      flush on the next allowable edit window.
- [ ] State-machine unit tests with a stubbed Telegram
      client. Cover: simple 1-tool turn, multi-tool turn,
      turn with approval, approval rejection, tool error,
      short chat turn.
- [ ] Timeline-ordering regression test: the final reply
      message's Telegram timestamp is strictly later than
      the approval bubble's timestamp. Pins the 2026-05-12
      bug.
- [ ] SSE reconnect test: transient network drop triggers
      backoff-reconnect; in-flight turn's state survives or
      is cleanly dropped on a fresh connect (decide: we
      probably drop it and rely on the gateway-side JSONL
      being the source of truth; a future admin dashboard
      can reconstruct from disk).
- [ ] Failure-mode tests: Telegram API error on post/edit
      logs a warning and the turn continues.

**Exit criteria:** a multi-step Telegram turn renders as the
documented per-action bubble sequence; the 2026-05-12
"approval floats in the wrong place" bug is demonstrably
fixed. `ruff` / `mypy` / `pytest` clean.

## Sub-phase 4.8d — `fitt watch` CLI renderer

- [ ] Create `gateway/src/gateway/cli_watch.py` with the
      renderer and tail loop.
- [ ] Implement the format from design.md § "CLI `fitt
      watch`". Fixed-width kind column, key-sorted meta
      rendering, two-level dict flatten with `{...}`
      truncation.
- [ ] Color via `rich` (already a dependency); ok=green,
      warn=yellow, error=red per design.md.
- [ ] Tail loop uses `TurnLog.read(session_key, since=...)`
      with a two-second sleep between polls; at midnight,
      switches to the next day's file via a `date.today()`
      check.
- [ ] `fitt watch <session>` and `fitt watch
      --session-active` (latest-by-turn).
- [ ] Works under `docker compose exec gateway fitt watch
      ...` — test that path.
- [ ] Output-format unit tests with synthetic
      `turns/<date>.jsonl` files.
- [ ] Tail-behavior test with a file being appended by a
      background task.

**Exit criteria:** `fitt watch` renders a live session's
events line-by-line, updates within 2s of a new event
landing. Clean exit on Ctrl-C.

## Sub-phase 4.8e — Static HTML viewer

- [ ] Create `gateway/src/gateway/viewer.py` with the HTML
      page as a module string. HTMX from CDN.
- [ ] Route `GET /v1/events/view?token=<token>` (with
      redirect-from-untokened-URL behaviour if we implement
      that — see open question 4).
- [ ] Inline CSS (readable on phone, monospace for meta).
- [ ] `hx-get` polling every 5s, append to top of list.
      (HTML viewer is the simpler proxy; it polls the JSON
      endpoint rather than consuming SSE. A future HTMX+SSE
      upgrade is a follow-up if phone browsers make polling
      visibly laggy.)
- [ ] Meta-detail expand-in-place per row.
- [ ] XSS-safe: HTML-escape all `meta` values before
      insertion.
- [ ] Smoke test: endpoint returns 200, body contains the
      expected list container + script block.
- [ ] Test that XSS attempt in an event's `meta.body`
      renders as escaped text.

**Exit criteria:** load
`http://<hub-tailnet>:8080/v1/events/view?token=...` on a
phone, see events landing every 5 seconds. Survives a
crafted event with `<script>` in its body.

## Deferred: Telegram `/inbox` historical browser

Originally sub-phase 4.8e. Deferred to post-v1 because the
live renderer (4.8b) covers the "see what FITT just did"
case that matters more. Revisit if scrolling past turns on
the phone becomes an actual daily friction. Scope if we
do return to it:

- `/inbox` command with optional filter flags (`cron`,
  `errors`, `session=<id>`).
- Paged view: 10 events per message, inline keyboard for
  navigation.
- Reads from `GET /v1/events` (landed in 4.8c) using the
  bot's service token.

## Cross-cutting

- [x] Update `FITT_ROADMAP.md` Phase 4.8 entry to reference
      this spec directory. *Shipped.*
- [ ] Log the shipping of each sub-phase in
      `docs/observed-issues.md` as its pain points surface
      fixes.
- [x] Resolve the open questions from design.md §
      "Open questions" → "Decisions". *Shipped commit
      `ecddead`.*
