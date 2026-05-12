# HTTP API reference

The gateway exposes a small HTTP surface on `:8080`, bound to
the Tailscale interface. All endpoints require a Bearer token
from `~/.fitt/secrets.yaml` unless noted.

## Chat

### `POST /v1/chat/completions`

OpenAI-compatible chat completions. Aliases in the `model`
field (`fitt-default`, `fitt-smart`, `fitt-fast`, ...) route to
concrete models per `config.yaml`. Concrete model IDs are
rejected with HTTP 400 — the alias indirection is the whole
point.

See the roadmap for streaming, memory, and tool-calling
behaviour. Session selection via `X-FITT-Session: <id>`.

## Observability (Phase 4.8c)

Four read-only endpoints expose the gateway's observability
logs over HTTP. All four return `{entries, next_since}` with
cursor-based pagination:

- **`since=<ts>`** (float, unix seconds) — exclusive
  lower-bound filter. A poller that just saw `ts=T` passes
  `since=T` on the next request and won't see that entry
  again.
- **`limit=<n>`** (int, 1-1000, default 100) — cap on
  response size.
- **`next_since`** in the response — the `ts` of the last
  returned entry, or `null` when the caller has reached the
  tail (response contained fewer than `limit` entries).

### `GET /v1/events`

User-visible event log (`events.jsonl`). What the Telegram
push channel consumes and what `fitt inbox` shows.

Filters:
- `kind=<kind>` — exact event kind (`cron_fired`,
  `cron_completed`, `approval_requested`, ...).

### `GET /v1/audit`

Tool-call audit log (`audit.jsonl`). HMAC-chained,
tamper-evident; each entry carries its own `hmac` and
`prev_hmac` so a consumer that wants to verify can do so
independently.

Filters:
- `tool=<name>` — exact tool-name match.

Verification (`fitt audit verify`) stays a CLI concern so a
polling subscriber can't DoS the gateway by demanding re-
verification every tick.

### `GET /v1/capability-gaps`

Ranked backlog of "I'd need a tool to X" gap reports.

Two modes:

- **Default** — paged raw feed, same shape as the others.
- **`ranked=true`** — grouped by canonicalised action,
  counted, with the most recent `ts` and `suggestion` per
  group. Response shape `{ranked: [...]}`; no cursor.

### `GET /v1/sessions/{session_id}/turns`

Per-turn event stream for one session
(`sessions/<id>/turns/<YYYY-MM-DD>.jsonl`). Produced by
Phase 4.8a's instrumentation: every LLM dispatch, every
tool call, every approval.

Filters:
- `kind=<kind>` — one of the
  `TURN_EVENT_KINDS`: `turn_started`, `llm_call_started`,
  `llm_call_completed`, `tool_call_planned`,
  `tool_call_executed`, `approval_requested`,
  `approval_decided`, `gap_reported`, `turn_finished`.
- `turn_id=<uuid>` — scope to one turn's events.

### `GET /v1/sessions/{session_id}/turns/stream`

Server-sent-events live stream of the same per-turn events.
Used by the Telegram live-turn renderer (Phase 4.8b).

- **Headers**: `Accept: text/event-stream`
- **Query**: `since=<ts>` optional; when set, the handler
  replays events newer than the cursor before flipping to
  live delivery.
- **Frame shape**:
  ```
  event: <kind>
  data: {"ts": ..., "kind": ..., "turn_id": ..., ...}
  
  ```
  Clients can subscribe to specific event kinds via
  `EventSource.addEventListener("tool_call_executed", ...)`.
- **Heartbeat**: `: heartbeat\n\n` every 15s to keep the
  connection alive and detect dead peers.
- **Disconnect**: Starlette cancels the handler's generator
  when the client hangs up. Subscribers are cleaned up in
  the generator's `finally`.

Example with curl:

```bash
curl -N -H "Authorization: Bearer $TOKEN" \
  "http://hub.tailnet:8080/v1/sessions/main/turns/stream?since=$LAST_TS"
```

## Approvals

See `gateway/README.md` for the full approval-flow
documentation. HTTP surface:

- `GET /v1/approvals/pending`
- `POST /v1/approvals/{id}/decide`

## Health

- `GET /health` — liveness probe. Returns 200 with
  `{"status": "ok"}` when the app is up. Unauthenticated.
