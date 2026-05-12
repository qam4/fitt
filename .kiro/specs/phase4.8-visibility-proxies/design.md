# Phase 4.8 — Visibility Proxies: Design

## Architecture overview

Five surfaces over one new backend. The backend — a per-turn
event stream persisted to JSONL — is the shape everything
else consumes. The surfaces are thin renderers:

```
                          ┌─ fitt watch (CLI)
                          │
turns.jsonl  →  TurnLog  ─┼─ GET /v1/sessions/<id>/turns  (HTTP JSON)
 writer + reader          │                              ↓
                          ├─ GET /v1/events/view          (HTML viewer)
                          │
events.jsonl reader  ────┤
audit.jsonl reader   ────┼─ GET /v1/events / /v1/audit / /v1/capability-gaps
capability_gaps log  ────┤
                          │
                          └─ Telegram /inbox command
```

The four existing logs (`events.jsonl`, `audit.jsonl`,
`capability_gaps.log`, per-session `history/*.md`) already
exist; the CLI already reads them. Phase 4.8 adds the HTTP
surface over these AND the new per-turn stream.

## Modules and responsibilities

New files:

- **`gateway/src/gateway/turns.py`** — per-turn event log
  primitive. `TurnEvent` dataclass (kind, timestamp, session,
  meta), `TurnLog(path)` with `append()` and `read()`, default
  path helper. Mirrors `gateway.events.EventLog` closely; the
  two primitives are deliberately similar because they have
  the same file posture (append-only JSONL, mtime-based
  liveness, no in-memory cache).
- **`gateway/src/gateway/events_endpoint.py`** — already
  exists as a module name. Verify scope and extend if
  necessary; if it currently covers only events push, add the
  read endpoints here.
- **`gateway/src/gateway/viewer.py`** — static HTML page
  embedded as a module constant. Serves from
  `events_endpoint.py`'s router.
- **`gateway/src/gateway/cli_watch.py`** — renderer for
  `fitt watch`. Tails `turns.jsonl`, formats each event
  line-by-line.

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

## HTML viewer

`GET /v1/events/view?token=<token>` returns a ~200-line
self-contained HTML page.

- `<head>` includes HTMX from the CDN (or an embedded build
  if we want to stay network-boundary-pure; start with CDN).
- `<body>` has a list container and a tiny controls bar:
  refresh interval, kind filter, sessions selector.
- `hx-get="/v1/events?since={{last_ts}}&limit=50"
  hx-trigger="every 5s"` appends new entries to the top of
  the list.
- One row per entry: timestamp, kind, session, one-line
  summary. Click for meta detail (expand in-place).
- CSS inline; goal is "readable on a phone browser at arm's
  length," not "matches FITT's brand identity."

The page uses JavaScript minimally — HTMX covers the polling;
a tiny function keeps a running high-watermark `last_ts`.

## Telegram `/inbox`

Wired via the bot's existing command handler framework. One
new handler function in `handlers.py`. Talks to
`GET /v1/events` (same endpoint humans would hit) with the
bot's service token. Formats via the existing event-push
formatters that already render to Telegram HTML.

Pagination: 10 events per message, navigation via inline
keyboard (`⬅️ Older` / `➡️ Newer`).

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

### 4.8a (backend)

- Unit tests for `TurnLog.append` / `read` mirror
  `test_events.py` structure. Property-test round trips
  (hypothesis, marked `# Phase 4.8, Property P1`).
- Unit tests for emission helpers — assert event shape and
  that IO failures become warnings, not exceptions.
- Integration test: full agent-loop turn with a stubbed LLM
  and tool; assert the expected event sequence lands in
  `turns.jsonl`.

### 4.8b (`fitt watch`)

- Output-format tests against synthetic `turns.jsonl` files.
- Tail-behavior tests with a file being appended by a
  background task.

### 4.8c (HTTP)

- Endpoint tests using `TestClient`. Auth required; pagination
  correct; `since` filters correctly.
- `/v1/sessions/<id>/turns` returns only the requested session.

### 4.8d (HTML viewer)

- Smoke: endpoint returns 200, HTML contains the list
  container, script block, expected token-query-param read.
- Check for obvious XSS: event `meta` gets HTML-escaped by the
  template.

### 4.8e (Telegram `/inbox`)

- Handler unit tests with a stubbed bot and a test HTTP
  client.
- Integration test: post `/inbox`, see a message with
  recent events.

## Migration

No migration. Turn logs are new files; existing sessions get
them the first time a turn runs under 4.8a-or-later.

## Sub-phase decomposition

- **4.8a** — `turns.py`, emission helpers, wiring into chat +
  cron. ~2 days.
- **4.8b** — `fitt watch` CLI. ~1 day.
- **4.8c** — HTTP read endpoints for events/audit/caps/turns.
  ~1 day.
- **4.8d** — Static HTML viewer. ~half-day.
- **4.8e** — Telegram `/inbox`. ~half-day.

Total: ~5 days focused work. Each sub-phase is independently
useful and independently testable.
