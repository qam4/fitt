# Phase 4.8 — Visibility Proxies: Design

## Architecture overview

Four surfaces over one new backend. The backend — a per-turn
event stream persisted to JSONL, with an in-process pub/sub
hook for live subscribers — is the shape everything else
consumes. The surfaces are thin renderers:

```
                              ┌─ fitt watch (CLI)            (tails JSONL)
                              │
turns/<YYYY-MM-DD>.jsonl      ├─ GET /v1/sessions/<id>/turns (HTTP JSON, paged)
  ▲                           │
  │ append()                  └─ GET /v1/sessions/<id>/turns/stream  (SSE)
  │                                                 │
TurnLog  ─── subscribers ─────────────────────────── │
                                                    ▼
                                            Telegram live-turn renderer
                                                (consumes SSE)

events.jsonl / audit.jsonl / capability_gaps  ── HTTP read endpoints
```

Two paths out of `TurnLog`:

1. **JSONL file**, for everyone who can wait (`fitt watch`,
   the paged HTTP endpoint, the future admin dashboard).
   Readers poll or tail the file.
2. **In-process pub/sub hook**, fanned out over HTTP SSE for
   external subscribers (the Telegram bot). The gateway's
   SSE handler registers a `TurnLog` subscriber and forwards
   each event to connected HTTP clients. The in-process
   hook is not exposed for out-of-process consumers —
   everything goes through the HTTP boundary so there's no
   tech debt from "bot imports gateway in-process" to unwind
   later.

## Modules and responsibilities

New files:

- **`gateway/src/gateway/turns.py`** — per-turn event log
  primitive. `TurnEvent` dataclass (kind, timestamp, session,
  meta), `TurnLog(path)` with `append()` and `read()`, default
  path helper. Also exposes `subscribe(callback)` and fires
  every subscriber on `append()` after the write flushes.
  Mirrors `gateway.events.EventLog` closely on the JSONL side;
  adds the pub/sub hook for live consumers.
- **`gateway/src/gateway/events_endpoint.py`** — already
  exists as a module name. Verify scope and extend if
  necessary; if it currently covers only events push, add the
  read endpoints here.
- **`gateway/src/gateway/cli_watch.py`** — renderer for
  `fitt watch`. Tails `turns/<date>.jsonl`, formats each
  event line-by-line.
- **`telegram-bot/src/fitt_telegram_bot/turn_renderer.py`** —
  the live-turn renderer. Subscribes to the gateway's
  turn-event stream over SSE (`GET /v1/sessions/*/turns/stream`
  from 4.8c), maintains per-turn state (`{tool_call_id →
  telegram_message_id, approval_id → telegram_message_id,
  final_reply_message_id}`), posts / edits / locks
  Telegram messages as events arrive.

Extensions to existing files:

- **`gateway/src/gateway/agent_loop.py`** — emission helpers
  `record_turn_started`, `record_llm_call`, `record_tool_call`,
  `record_turn_finished`. Called at the existing points the
  loop already logs / emits events.
- **`gateway/src/gateway/chat.py`** — `_run_tool_loop` opens
  a `TurnLog` per request (or reuses a shared one), passes it
  to `run_agent_loop` via a new `turn_log` parameter, and
  closes it (no-op — no open file handles held) at the end.
- **`gateway/src/gateway/cron_runner.py`** — same pattern.
- **`gateway/src/gateway/approval.py`** — emits
  `approval_requested` / `approval_decided` events.
- **`gateway/src/gateway/cli.py`** — new `watch` command +
  maybe-future `turns` group.
- **`telegram-bot/src/fitt_telegram_bot/handlers.py`** — new
  `/inbox` command handler calling the events endpoint.

## On-disk format

### `turns.jsonl`

Path: `$FITT_HOME/sessions/<session_key>/turns/<YYYY-MM-DD>.jsonl`.

One file per session per day — same shape as
`history/<YYYY-MM-DD>.md`. A turn that crosses midnight
writes later events to the next day's file; readers that want
a full turn stitch adjacent days (uncommon in practice; chat
turns finish in seconds). The pruner reuses its existing
date-parsing logic; no new retention knob.

One JSONL line per event. Schema:

```
{
  "turn_id": "uuid4-for-the-turn",
  "event_id": "uuid4-for-this-event",
  "kind": "turn_started | llm_call_completed | ...",
  "ts": "2026-05-11T12:00:00.123456Z",
  "session_key": "main",
  "meta": { ... kind-specific fields ... }
}
```

### Event-kind schemas

- `turn_started` — `meta`: `client`, `alias`, `user_msg_len`.
- `llm_call_started` — `meta`: `alias`, `iteration`.
- `llm_call_completed` — `meta`: `model`, `latency_ms`,
  `in_tokens`, `out_tokens`, `finish_reason`,
  `tool_calls_count`, `cost_usd`.
- `tool_call_planned` — `meta`: `tool_name`, `args`,
  `call_id`, `iteration`. `args` is the full dict (not a
  summary); `turns.jsonl` isn't chat history and doesn't need
  to worry about context bloat.
- `approval_requested` — `meta`: `approval_id`, `tool_name`,
  `bucket`, `client`.
- `approval_decided` — `meta`: `approval_id`, `decision`
  (`approve|reject|trust_session|timeout`), `duration_ms`.
- `tool_call_executed` — `meta`: `tool_name`, `call_id`,
  `ok`, `duration_ms`, `result_summary` (≤300 chars),
  `artifact_path` (if hoisted). Exit code surfaced for
  `project_shell`.
- `gap_reported` — `meta`: `gap_text`, `suggestion`.
- `turn_finished` — `meta`: `status` (`ok|upstream_error|
  tool_loop_exhausted`), `iterations`, `final_reply_len`.

### Decay

The history pruner's existing walk (`sessions/<k>/history/*.md`
+ `sessions/<k>/artifacts/<YYYY-MM-DD>/` from the tool-artifact
commit) extends to `sessions/<k>/turns/<YYYY-MM-DD>.jsonl` —
same date-parsing logic, same retention window, no new knob.

## Correctness properties

- **P1: Write-once semantics per event.** Every
  `TurnLog.append` writes exactly one line to disk and flushes.
  Never two lines, never a partial line.
- **P2: Events are ordered within a turn.** Sequential append
  on a single file from one process; no locking needed (single
  writer is the gateway's chat loop for a given turn).
- **P3: IO failure is non-fatal.** An unwritable turns file
  logs a warning and the turn continues. Lost visibility is
  worse than stopped FITT.
- **P4: Concurrent sessions don't collide.** Each session has
  its own per-day file; cross-session events (cron firings,
  late-tool results) write to their own session's file.
- **P5: Read is consistent.** The reader opens the file,
  seeks to `since` (via a stored offset cache), parses to
  EOF. Concurrent appends land below the read's cursor and
  appear in the next read.
- **P6: Schema is additive.** Adding new event kinds or new
  `meta` fields on existing kinds is never a breaking change.
  Clients (CLI, HTML viewer) ignore unknown kinds and fields.
- **P7: Redaction is explicit.** `args` on `tool_call_planned`
  contains the model's arguments verbatim, including anything
  the model invented. Secrets leaked into args are a separate
  problem (fixed at the tool layer, not the log layer); the
  log faithfully records what happened.

## Emission sites

Wired at the existing call points rather than a separate
instrumentation layer. Each site already logs a line or emits
an event; we're adding one `TurnLog.append(...)` alongside.

- **`run_agent_loop`** at `for iteration in range(...)`:
  `record_llm_call_started` before dispatch,
  `record_llm_call_completed` after.
- **`execute_tool_call`** at the start:
  `record_tool_call_planned`; at the end:
  `record_tool_call_executed`.
- **`ApprovalMiddleware.request_approval`**:
  `record_approval_requested`.
- **`ApprovalMiddleware.resolve_approval` / timeout branch**:
  `record_approval_decided`.
- **`record_gap`**: same.
- **`_run_tool_loop` in chat.py**: `record_turn_started` at
  entry, `record_turn_finished` at the single return point.
- **`CronRunner.fire`**: same turn bracket as chat.

Turn id flows via `ToolContext.turn_id: str | None` added in
this phase. The chat handler generates it once per request
before calling `run_agent_loop`. Absent turn id means
"Phase 4.8 logging off" — tests and early-phase callers get
a no-op.

## CLI `fitt watch`

```
$ fitt watch main
12:00:01  turn_started        client=telegram alias=fitt-smart
12:00:01  llm_call_started    iter=1
12:00:02  llm_call_completed  iter=1 model=deepseek-v4-flash latency=920ms in=412 out=35 fr=tool_calls
12:00:02  tool_call_planned   read_file(project="hub", path="README.md") call=c1
12:00:02  tool_call_executed  read_file ok duration=15ms
12:00:02  llm_call_started    iter=2
12:00:03  llm_call_completed  iter=2 model=deepseek-v4-flash latency=840ms in=580 out=92 fr=stop
12:00:03  turn_finished       status=ok iterations=2 reply_len=412
```

Format rules:

- Timestamp in local TZ, HH:MM:SS precision.
- Kind in a fixed column width (18).
- `meta` rendered key-sorted, `key=value` join. Dicts
  (like `args`) flatten into `key.subkey=value`; nested
  dicts deeper than two levels abbreviate to `{...}`.
- Color: ok=green, warn=yellow (narrated, gap,
  approval_timeout), error=red (upstream_error,
  loop_exhausted).

Implementation: a tail loop that reads new lines from the
file, formats them, prints. Uses `TurnLog.read(since=<last>)`
with a two-second sleep between polls. `Ctrl-C` to exit.

## HTTP endpoints

Mounted on the existing FastAPI app under `events_endpoint.py`
(rename to something broader if it's currently scoped to push;
`read_endpoints.py` is a cleaner name).

- **GET /v1/events** — wraps `EventLog.read()`. Query params:
  `since`, `kind`, `session_key`, `limit` (default 50, max
  500). Cursor is the entry's `ts` — client re-requests with
  `since=<last_ts>`.
- **GET /v1/audit** — wraps `AuditLog.iter_entries()`. No
  verification — that's CLI-only. Query params: `since`,
  `limit`, `tool`.
- **GET /v1/capability-gaps** — calls the same aggregator the
  `fitt capability-gaps` CLI uses. Returns the ranked list.
- **GET /v1/sessions/{id}/turns** — wraps `TurnLog.read(...)`
  for one session. Same `since` / `limit` / `kind` shape as
  `/v1/events`.

Shared behaviour:

- Bearer auth via the existing `AuthMiddleware`. 401 on bad
  token, 403 on token with insufficient tag (future; not in
  this phase).
- JSON response `{entries: [...], next_since: "<ts>" | null}`.
  `next_since` is null when the response contains fewer than
  `limit` entries (caller has reached the tail).
- ISO 8601 UTC timestamps.

## Deferred: HTML viewer / barebone dashboard

The original 4.8e shipped a single-file HTML page
(`GET /v1/events/view`) with HTMX polling as a minimum-
viable dashboard. Decided 2026-05-13 to drop it from Phase
4.8 and do the full admin dashboard in Phase 7+ instead —
the daily phone surface is Telegram (4.8b), a half-day
stepping-stone dashboard would need to be maintained
alongside the real one once it ships, and the audience
that wants "browse $FITT_HOME from a browser" is better
served by the editable session-browser version when it's
ready.

## Telegram live-turn renderer

Wired via the bot's existing command handler framework plus a
new in-process subscriber on the gateway's `TurnLog`. One new
module (`telegram-bot/src/fitt_telegram_bot/turn_renderer.py`)
holds the state machine; existing bot plumbing (`bot.py`,
`handlers.py`) stays thin.

### Subscription model

The gateway and bot run in separate containers in today's
compose topology. The bot subscribes to the gateway's turn
events over SSE: a long-lived `httpx.AsyncClient.stream`
connection to `GET /v1/sessions/*/turns/stream` that the
gateway keeps open until the client hangs up. The gateway-
side handler uses `TurnLog.subscribe()` as the in-process
fanout mechanism — each subscriber callback places the event
on its connection's queue; the SSE handler drains the queue
and writes `data: <json>\n\n` frames to the client.

The bot reconnects with exponential backoff on disconnect.
If the gateway restarts mid-turn, the bot's in-memory render
state is dropped on reconnect and the next event stream
begins fresh. The gateway-side JSONL is the source of truth;
a future admin dashboard can reconstruct past turns from
disk.

### Per-turn state machine

On `turn_started`, the renderer creates an in-memory
`TurnRenderState`:

```python
@dataclass
class TurnRenderState:
    turn_id: str
    chat_id: int
    stream_message_id: int | None = None
    # The growing bubble. Lazy-created on first tool call
    # or first streamed narration token.
    stream_text: str = ""
    # Accumulated content of the growing bubble. New task
    # cards get appended as lines; final reply tokens get
    # appended too. Edits push this whole string to
    # Telegram at ~1/sec rate limit.
    approval_bubbles: dict[str, int] = field(default_factory=dict)
    # approval_id -> telegram message_id
    finish_footer_message_id: int | None = None
    last_stream_edit_ts: float = 0.0  # rate-limit coalescing
```

Three bubble types, three event handlers that post them:

**Growing stream bubble** (one per turn, created lazily):

- `tool_call_planned` → append "🔵 Reading X…" to
  `stream_text`, edit the stream message (or post it if
  this is the first append).
- `tool_call_executed` → find the matching "🔵 Reading X…"
  line in `stream_text` and rewrite it to "✅ Read X (Nms)"
  (success) or "❌ Read X — error" (failure). Edit the
  stream message.
- `llm_call_completed` / narration tokens → append the
  token to `stream_text`, edit at most every 1 second per
  rate-limit budget.
- Edits on the stream message are always silent
  (`disable_notification=true`). The message's send
  timestamp stays at `turn_started`.

**Approval bubbles** (one per approval, zero to many):

- `approval_requested` → post a NEW message with the
  ✅ / ❌ / 🔓 inline keyboard. Notifies. Store
  `approval_bubbles[approval_id] = message_id`.
- `approval_decided` → edit that specific message to
  "🔐 edit_file → ✅ Approved" (or the corresponding
  outcome). Buttons clear. The approval bubble stays at
  its posted timestamp forever.

**Finish footer** (one per turn, at `turn_finished`):

- `turn_finished` → post a NEW message. Content is short:
  "✓ Finished in Ns" (success), "🚫 Rejected" (turn
  cancelled), or "⏱️ Timed out" (tool loop exhausted).
  Notifies. This is the turn-done ping for the phone.

**Final reply** is a separate concern. It flows through
the existing chat streaming path in `chat.py` — tokens get
streamed to the Telegram client, which the renderer has
already set up as the growing stream bubble. No new code
in the renderer posts the final reply; it lands in the
growing bubble naturally. `turn_finished` fires after the
final reply is complete.

### Rate limiting

Telegram's edit-message cap is ~1 edit per second per
chat. The growing stream bubble gets the heaviest edit
traffic (every tool_call_planned, every tool_call_executed,
every narration token). State tracks `last_stream_edit_ts`
and coalesces: if an event would land within 1 second of
the last edit, buffer the text and schedule a flush after
the remaining window. In practice this means a burst of 5
task cards within 1 second shows up as one or two
edit frames with all 5 cards, not 5 separate edit frames.

Approval bubbles are new messages, rate-limited against
Telegram's "messages per second" cap (30/sec/chat) which
we never approach in practice.

The finish footer is one new message, no rate concern.

### Short-chat-turn detection

A turn where no `tool_call_planned` and no
`approval_requested` event fires skips the growing-bubble
machinery entirely. `turn_started` creates the in-memory
state but posts nothing. The existing chat streaming path
posts the model's reply as a standalone message (today's
behaviour). On `turn_finished`, if `stream_message_id` is
still `None`, no finish footer is posted either — the
reply message IS the turn's ping.

### Failure modes

- **Telegram API error on message post/edit:** log a
  warning, mark the bubble as "failed to render," keep the
  turn going. Losing a status bubble doesn't invalidate
  the turn.
- **Bot process restart mid-turn:** the in-memory state is
  lost. The agent loop completes; the turn's events still
  land on disk via `turns/<date>.jsonl`. The bot reconnects
  to the SSE stream and starts fresh — it won't
  retroactively render the in-flight turn it missed. A
  future admin dashboard can show past turns by reading
  the JSONL directly.
- **Telegram bubble length overflow (4096 chars):** for
  v1, log a warning and stop appending to the stream. A
  follow-up can add MeshClaw-style stream rotation (post a
  new bubble, keep streaming into it) if it becomes a real
  problem.
- **Slow subscriber / SSE disconnect:** the bot's SSE
  client reconnects with exponential backoff. Events that
  fire during the disconnect window are on disk but not
  rendered live — the bot picks up from the current tail
  on reconnect, accepting that it missed the middle of a
  turn. The JSONL stays the source of truth.

## Deferred: `/inbox` historical browser

The original `/inbox` design (paged browser over
`events.jsonl`) ships post-v1 if it's still wanted after the
live renderer is in use. Principle 9: live with the live
renderer first; if scrolling past turns on the phone becomes
a real need, add it as a targeted follow-up.

## Config

```yaml
# (no new block — the turns log reuses the history
# retention knob; artifact hoisting introduced
# memory.tool_output_* for its own thresholds)
memory:
  history_max_days: 90  # already exists; now also governs turns/<date>.jsonl
```

## Decisions

Resolved before 4.8a implementation started (2026-05-12):

1. **Rotation: per-day, matching history.** Path is
   `sessions/<k>/turns/<YYYY-MM-DD>.jsonl`. Pruner reuses
   the existing filename-date walk for
   `history/<YYYY-MM-DD>.md`. No per-size rotation; no
   `turns.jsonl` single-file-per-session variant. A turn
   that crosses midnight writes later events to the next
   day's file. If real use ever exceeds 100 MB per session
   per day, reconsider.
2. **No `limit=0` HEAD query on HTTP.** Not in MVP; add only
   if the HTML viewer's first-load cost shows up as a
   problem.
3. **`fitt watch --follow` across day rotation.** The tailer
   watches today's file; at midnight it switches to the
   next day. Simple `date.today()` check before each poll;
   no inotify/watchdog.
4. **HTML viewer auth: `?token=<token>` query param.**
   Single-operator deployment; OAuth / basic-auth is
   over-engineering. Tokens are the existing bearer tokens
   from `secrets.yaml`.
5. **Config location: reuse `memory.history_max_days`.** No
   new `turns:` block. Turn logs are session-scoped detail;
   they share retention with history.

## Migration

No migration. Turn logs are new files; existing sessions get
them the first time a turn runs under 4.8a-or-later.

## Testing strategy

Per sub-phase:

### 4.8a (backend + pub/sub)

- Unit tests for `TurnLog.append` / `read` mirror
  `test_events.py` structure. Property-test round trips
  (hypothesis, marked `# Phase 4.8, Property P1`).
- Unit tests for emission helpers — assert event shape and
  that IO failures become warnings, not exceptions.
- Unit tests for the pub/sub hook: a registered callback
  fires once per append; a raising callback gets its error
  logged and doesn't break the append or other subscribers.
- Integration test: full agent-loop turn with a stubbed LLM
  and tool; assert the expected event sequence lands in
  `turns/<date>.jsonl` AND reaches subscribers in the same
  order.

### 4.8b (Telegram live-turn renderer)

- State-machine unit tests with a stubbed Telegram client:
  tool-call planned + executed → one silent bubble edited
  once; approval lifecycle → one notifying bubble edited
  to outcome; short-chat turn → no action bubbles.
- Rendering tests for message text: icon choices, duration
  formatting, error-case text.
- Timeline-ordering regression test: under the new renderer,
  given a turn with an approval in the middle, the final
  reply message's timestamp is strictly later than the
  approval's timestamp. Pins the 2026-05-12 Telegram bug.
- Failure-mode tests: Telegram API error on post/edit logs
  a warning and the turn continues; mid-turn restart
  doesn't leave a broken state.

### 4.8c (HTTP read endpoints + SSE stream)

- Endpoint tests using `TestClient`. Auth required; pagination
  correct; `since` filters correctly.
- `/v1/sessions/<id>/turns` returns only the requested session.
- SSE endpoint: delivers appended events, replays from
  `since`, disconnects cleanly when the client hangs up.

### 4.8d (`fitt watch`)

- Output-format tests against synthetic `turns/<date>.jsonl`
  files.
- Tail-behavior tests with a file being appended by a
  background task.

## Migration

No migration. Turn logs are new files; existing sessions get
them the first time a turn runs under 4.8a-or-later.

## Sub-phase decomposition

- **4.8a** — `turns.py` primitive (shipped 2026-05-12 as
  commit `0d974c8`), subscribe hook (shipped `bb05a2d`),
  emission helpers (shipped `30f182a`), wiring into chat +
  cron + approval, TurnLog lifecycle in app.py. ~1 day
  remaining (primitive + hook + helpers done; wiring +
  integration test still open).
- **4.8c** — HTTP read endpoints including SSE stream for
  turns. Promoted ahead of 4.8b because the bot consumes
  the stream over HTTP. ~1 day.
- **4.8b** — Telegram live-turn renderer (consumes SSE).
  High-impact mobile piece. ~1½ days.
- **4.8d** — `fitt watch` CLI (developer / debugging
  tool, not daily surface). ~1 day.

Total: ~4½ days remaining focused work. HTML viewer (former
4.8e) dropped; real admin dashboard lives in Phase 7+.
