# Phase 4.8 — Visibility Proxies: Tasks

Five sub-phases. 4.8a is the dependency for everything else.
Order after 4.8a is 4.8b (Telegram live-turn renderer) first
for high-impact mobile reach, then operator surfaces (CLI,
HTTP, HTML) in any order.

## Sub-phase 4.8a — Per-turn event stream backend + pub/sub

- [x] Create `gateway/src/gateway/turns.py` with
      `TurnEvent` dataclass, `TurnLog` class (append, read,
      file_path, new_event). Per-session-per-day layout at
      `sessions/<k>/turns/<YYYY-MM-DD>.jsonl`.
      *Shipped commit `0d974c8`.*
- [x] Pin the event-kind schemas (`TURN_EVENT_KINDS`).
      *Shipped in `turns.py`.*
- [ ] Add `TurnLog.subscribe(callback)` and fire registered
      callbacks after each successful append. Raising
      callbacks get logged, never break persistence.
- [ ] Add `turn_id: str | None = None` to `ToolContext`.
- [ ] Wire `record_turn_started` / `record_turn_finished` in
      `chat.py` around the tool-loop entry.
- [ ] Wire `record_llm_call_started` / `record_llm_call_completed`
      in `agent_loop.py` around the dispatch call.
- [ ] Wire `record_tool_call_planned` in `execute_tool_call`
      at entry; `record_tool_call_executed` at return.
- [ ] Wire `record_approval_requested` in
      `ApprovalMiddleware.request_approval`.
- [ ] Wire `record_approval_decided` in
      `resolve_approval` + timeout branch of
      `_request_and_wait`.
- [ ] Extend the existing `record_gap` in `agent_loop.py` to
      also emit turn events.
- [ ] Generate `turn_id` once per chat request in `chat.py`
      and on every cron firing in `cron_runner.py`; pass to
      the tool context.
- [ ] Handle IO failures as non-fatal warnings per P3.
- [ ] Hook the history pruner to walk
      `turns/<YYYY-MM-DD>.jsonl` via the same date-parsing
      code that already sweeps history and artifact dirs.
- [ ] Unit tests for the pub/sub hook: one append → one
      callback per subscriber; raising callback logged and
      others still fire; no subscribers = no-op.
- [ ] Integration test: full agent-loop turn with stubbed
      LLM produces the expected event sequence in both the
      JSONL file AND the subscriber callback list.
- [ ] IO-failure test: unwritable `turns/<date>.jsonl` logs a
      warning and the turn completes anyway (already
      covered in the shipped primitive tests; extend when
      emission sites exist).

**Exit criteria:** a tool-using turn in Telegram writes a
valid `turns/<date>.jsonl` with all the expected event kinds
in order; a registered in-process subscriber observes the
same events in the same order. `ruff` / `mypy` / `pytest`
clean.

## Sub-phase 4.8b — Telegram live-turn renderer

- [ ] Create
      `telegram-bot/src/fitt_telegram_bot/turn_renderer.py`
      with `TurnRenderState` dataclass and the event-to-
      Telegram-action state machine per design.md § "Telegram
      live-turn renderer".
- [ ] Subscribe to the gateway's `TurnLog` at bot boot.
- [ ] `tool_call_planned` → post silent message "🔵 …";
      record `tool_bubbles[call_id]`.
- [ ] `tool_call_executed` → edit in place to "✅ (Nms)" or
      "❌ — error"; lock message.
- [ ] `approval_requested` → post notifying message with
      ✅ / ❌ / 🔓 inline keyboard; record
      `approval_bubbles[approval_id]`.
- [ ] `approval_decided` → edit in place to outcome; buttons
      clear.
- [ ] `turn_finished` → drop the `TurnRenderState`; the
      final reply was (or will be) posted by the existing
      chat streaming path.
- [ ] Short-chat turn detection: if no tool or approval
      events fire between `turn_started` and
      `turn_finished`, no action bubbles are posted.
- [ ] State-machine unit tests with a stubbed Telegram
      client. Cover: simple 1-tool turn, multi-tool turn,
      turn with approval, approval rejection, tool error,
      short chat turn.
- [ ] Timeline-ordering regression test: the final reply
      message's Telegram timestamp is strictly later than
      the approval bubble's timestamp. Pins the 2026-05-12
      bug.
- [ ] Failure-mode tests: Telegram API error on post/edit
      logs a warning and the turn continues.

**Exit criteria:** a multi-step Telegram turn renders as the
documented per-action bubble sequence; the 2026-05-12
"approval floats in the wrong place" bug is demonstrably
fixed. `ruff` / `mypy` / `pytest` clean.

## Sub-phase 4.8c — `fitt watch` CLI renderer

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

## Sub-phase 4.8d — HTTP read endpoints

- [ ] Extend (or create) `gateway/src/gateway/events_endpoint.py`
      / rename to a broader name. Add routes for:
      - `GET /v1/events`
      - `GET /v1/audit`
      - `GET /v1/capability-gaps`
      - `GET /v1/sessions/{id}/turns`
- [ ] Pagination via `since=<ts>` / `limit=<n>`. Response
      shape `{entries, next_since}`.
- [ ] Auth via existing `AuthMiddleware`; no per-endpoint
      ACL in this phase.
- [ ] `TestClient` tests: happy path, 401 on bad token,
      `since` filtering correct, `limit` bounded,
      `next_since=null` at tail.
- [ ] Per-session filter for `/v1/sessions/{id}/turns`
      returns only that session's events.
- [ ] Document the endpoints in `README.md` or a new
      `docs/http-api.md`.

**Exit criteria:** a curl against each endpoint returns
well-formed JSON and matches the CLI's output for the same
filter. Tests clean.

## Sub-phase 4.8e — Static HTML viewer

- [ ] Create `gateway/src/gateway/viewer.py` with the HTML
      page as a module string. HTMX from CDN.
- [ ] Route `GET /v1/events/view?token=<token>` (with
      redirect-from-untokened-URL behaviour if we implement
      that — see open question 4).
- [ ] Inline CSS (readable on phone, monospace for meta).
- [ ] `hx-get` polling every 5s, append to top of list.
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
- Reads from `GET /v1/events` (landed in 4.8d) using the
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
