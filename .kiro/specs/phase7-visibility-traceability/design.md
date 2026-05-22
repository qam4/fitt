# Phase 7 — Visibility & Traceability: Design

## Architecture summary

Phase 7 layers visibility surfaces over the existing
gateway substrate. New code lands in five clusters,
matching the slice decomposition:

```
+-- gateway/src/gateway/ ------------------------------------+
|                                                            |
|  Slice 7.1 — context discovery                             |
|    context_window.py        — discovery + cache            |
|    aliases_endpoint.py      — GET /v1/aliases              |
|    cli.py                   — `fitt context refresh`       |
|                                                            |
|  Slice 7.2 — per-turn capture                              |
|    turn_capture.py          — sidecar JSON store           |
|    turn_capture_endpoint.py — GET /v1/sessions/.../captures|
|                               /<turn_id>                   |
|    chat.py                  — wire capture into the loop   |
|    history_pruner.py        — extend sweep to turns/<date> |
|    cli.py                   — `fitt turn show <id>`        |
|                                                            |
|  Slice 7.5 — dashboard v0                                  |
|    dashboard/__init__.py    — FastAPI sub-router           |
|    dashboard/templates/     — Jinja templates              |
|    dashboard/static/        — minimal CSS, htmx.min.js     |
|    dashboard/auth.py        — bearer-or-cookie middleware  |
|                                                            |
+------------------------------------------------------------+

+-- telegram-bot/src/fitt_telegram_bot/ ---------------------+
|                                                            |
|  Slice 7.3 — operator commands                             |
|    handlers.py              — /lastturn, /status, /eval    |
|    handlers.py              — /model enrichment            |
|                               (partial shipped 2026-05-22) |
|    gateway_client.py        — client methods for the new   |
|                               endpoints                    |
|                                                            |
|  Slice 7.4 — markdown renderer                             |
|    markdown_render.py       — CommonMark -> Telegram HTML  |
|    streaming.py             — apply at _flush time         |
|    handlers.py              — apply at command-response    |
|                               and event-push time          |
|                                                            |
+------------------------------------------------------------+
```

No new processes. No new daemons. The substrate is the
existing gateway HTTP surface plus the existing Telegram
bot; Phase 7 adds endpoints, modules, and views, not
infrastructure.

## Modules

### `gateway/context_window.py` (Slice 7.1)

Single module owning context-window discovery for every
backend. Per-backend implementations registered against a
common interface:

```python
class ContextWindowProbe(Protocol):
    backend: Backend  # ollama | openai | openrouter | anthropic
    async def discover(
        self,
        model: ModelConfig,
        secrets: Secrets,
    ) -> ContextWindowResult:
        ...
```

`ContextWindowResult` is a frozen dataclass with:

- `tokens: int | None` — discovered ceiling, or `None` on
  failure
- `source: Literal["modelfile", "model_info", "api_models",
  "lookup_table", "default", "unknown"]` — provenance for
  the operator
- `detail: str` — short human-readable explanation
- `discovered_at: float` — UNIX timestamp; cache key

A `ContextWindowCache` class holds per-binding results,
keyed by `(backend, model_id)`. Lifetime: one gateway
process. Refresh on `fitt context refresh` CLI hits a new
endpoint that re-runs discovery; otherwise results stick
through the process's lifetime.

#### Per-backend probe contracts

**Ollama** (`OllamaContextProbe`):

1. POST `{base_url}/api/show` with body `{"name": "<model>"}`.
2. Parse `parameters` field for `num_ctx <N>` line. If
   present, that's the effective context window.
3. Fall back to `model_info["<arch>.context_length"]` —
   architecture's natural ceiling.
4. Fall back to 2048 (Ollama default) and tag with
   `source="default"`. Log WARNING because that's the
   "operator forgot to set OLLAMA_CONTEXT_LENGTH" case.

**OpenAI-compatible** (`OpenAIContextProbe`, covers `openai`,
`openrouter`, NIM, Groq, Together):

1. GET `{api_base}/v1/models` with the configured api_key.
2. Find the matching model by id. Read `context_length` (or
   `max_input_tokens` for OpenRouter, which uses that name).
3. On 401: log ERROR, return `tokens=None`,
   `source="unknown"`. The api_keys check at boot already
   logged this once; we don't double-shout.

**Anthropic** (`AnthropicContextProbe`):

1. Static lookup table:
   ```python
   _ANTHROPIC_CONTEXT_BY_FAMILY = {
       "claude-sonnet-4": 200_000,
       "claude-opus-4": 200_000,
       "claude-haiku-4": 200_000,
       # extend as new families ship
   }
   ```
2. Match the configured model id against family prefix.
   Unmatched: `tokens=None`, `source="unknown"`. Adding a
   new Anthropic family is a one-line edit when it ships.

#### Probe lifecycle

Discovery runs once per binding at gateway boot, alongside
the existing `alias_probe` (Phase 4.9 boot-time tool-call
reliability probe). The two share the same posture: best
effort, log loud on failure, never block startup.

```
create_app()
    -> ContextWindowCache.populate(config, secrets)  # async
    -> stash on app.state.context_windows
```

The probe ordering is: api_keys check (existing), then
context-window discovery (new), then alias_probe (existing).
Total boot-time cost stays under ~10s typical.

### `gateway/aliases_endpoint.py` (Slice 7.1)

`GET /v1/aliases` returns:

```json
{
  "aliases": [
    {
      "id": "fitt-default",
      "primary": {
        "model_id": "qwen-coder-big",
        "model": "qwen2.5-coder:14b",
        "backend": "ollama",
        "endpoint": "http://laptop:11434"
      },
      "fallback": {"model_id": "..."},
      "context_window": {
        "tokens": 32768,
        "source": "modelfile",
        "detail": "ollama num_ctx parameter set",
        "discovered_at": 1779479823.42
      },
      "last_probe": {
        "status": "ok",
        "detail": "emitted 1 tool call(s) as expected",
        "ran_at": 1779479823.42
      },
      "last_eval": {
        "pass_rate": 0.8,
        "passed": 4,
        "total": 5,
        "ran_at": 1779479823.42
      }
    }
  ]
}
```

Notes:

- `last_probe` reads from the `alias_probe` results that
  the gateway captures at boot. If the boot probe is
  disabled (`server.boot_probe_enabled: false`) the field
  is `null`.
- `last_eval` reads the rolling per-alias eval report at
  `$FITT_HOME/eval/<alias>-latest.md`. Parses the markdown
  header for the pass-rate line. Absent file → `null`.
- Bearer auth via the existing middleware. Auth-exempt is
  reserved for `/v1/models` (the OpenAI-shape compatibility
  surface); `/v1/aliases` is FITT-internal and gated.

### `gateway/turn_capture.py` (Slice 7.2)

A small module owning per-turn body capture. The Phase 4.8
`TurnLog` already captures lifecycle events (turn_started,
llm_call_*, tool_call_*, approval_*, gap_reported,
turn_finished) as JSONL. This module adds *body* capture as a
sidecar JSON file per turn.

#### Storage shape

```
$FITT_HOME/sessions/<session>/turns/
  <YYYY-MM-DD>.jsonl                   # Phase 4.8: per-event log
  <YYYY-MM-DD>/<turn_id>.json          # Phase 7.2: per-turn body
```

The sidecar shape isn't strictly necessary — the events log
already has every field — but separating bodies from events
keeps the events log fast to tail (small lines, cheap JSON
parse) while putting bulky payloads (5K-token system prompts,
tool result strings) in their own file.

#### `TurnCapture` dataclass

```python
@dataclass(frozen=True, slots=True)
class TurnCapture:
    turn_id: str
    session_key: str
    alias: str
    client: str
    model_used: str
    backend: str
    fallback_used: bool
    started_at: float
    finished_at: float
    dispatched_messages: list[dict[str, Any]]  # OpenAI shape
    response: dict[str, Any]                    # last upstream resp
    tool_calls: list[CapturedToolCall]
    prompt_tokens: int
    completion_tokens: int
    context_window: int | None
    prompt_pct_of_window: float | None
    finish_reason: str | None
    narration_warning: bool
    iterations: int
    status: str  # ok / upstream_error / tool_loop_exhausted
```

```python
@dataclass(frozen=True, slots=True)
class CapturedToolCall:
    call_id: str
    tool_name: str
    args: dict[str, Any]
    decision: str  # ApprovalDecision.reason
    decision_detail: str
    duration_ms: int
    ok: bool
    result_summary: str
    artifact_path: str | None
    iteration: int
```

#### Write path

The chat handler (`chat.py::_run_tool_loop`) and the cron
runner already build everything in this dataclass piecemeal
during the turn. The change is to *aggregate* the pieces
into a `TurnCapture` and write once at turn-finished time.

Where the data comes from:

| Field | Source |
|---|---|
| `turn_id` | existing `tool_ctx.turn_id` |
| `dispatched_messages` | existing — captured in `working_messages` after final dispatch |
| `response` | existing `result.response_obj` (LiteLLM model dump) |
| `tool_calls` | existing `result.tool_calls_for_memory` (Phase 5) extended with approval decision detail |
| `prompt_tokens` / `completion_tokens` | existing `result.in_tokens` / `result.out_tokens` |
| `context_window` | new — looked up from `app.state.context_windows` by `(backend, model_id)` |
| `prompt_pct_of_window` | derived (`prompt_tokens / context_window * 100`) |
| `narration_warning` | new — runs `is_tool_use_expected_but_none` post-hoc on the captured response. Stored as a flag, not emitted as an event. See "Narration warning is a flag, not a signal" below. |

Write semantics:

- Atomic: write to `<turn_id>.json.tmp` then rename. Avoids
  half-written files if the process dies mid-write.
- Non-blocking: spawn a single asyncio task to write; the
  chat handler doesn't await it. Failure logs a warning.
- One-shot: write at `turn_finished` time only. We don't
  partially-write and update; partial writes are what events
  are for.

#### Privacy default by client

Capture defaults:

```python
_CAPTURE_BY_DEFAULT = frozenset({"telegram", "webui", "cli", "ide"})
# coding-agent: NOT in the default set
```

Operator can override via `traceability.default_capture` in
config.yaml — a list of client tags that get captured. Per-
session override is a follow-up; v0 ships with the per-client
default only.

#### Narration warning is a flag, not a signal

Background: 2026-05-12 we shipped a runtime
`tool_call_narrated` event from `record_narrated_tool_call`
that fired `is_tool_use_expected_but_none` on every chat
turn. It hit 100% false-positive rate on chit-chat. We rolled
it back. The classifier itself stayed valid for `alias_probe`
and `alias_eval` where the test author pinned the expected
outcome by construction.

Slice 7.2 brings the classifier back, but in a different role:

- **Not a runtime signal.** No event emitted on this
  classifier alone in the live chat path.
- **A captured flag.** Stored on `TurnCapture` as a boolean
  for after-the-fact triage. Operators looking at a
  surprising turn see "narration_warning: true" alongside
  the prompt size, model, finish reason. That tells them
  "the shape suggests narration; here's the prompt size;
  decide for yourself."
- **Surfaced in the dashboard turns view.** When the flag
  is true, the row badges with a "⚠ narration?" annotation
  linking to the captured detail.

This avoids the 2026-05-12 anti-pattern (gate live behaviour
on flimsy intent inference) while preserving the diagnostic
value the classifier offers when the operator is already
debugging a specific turn. See `docs/observed-issues.md`
for the rollback rationale we're respecting.

### `gateway/turn_capture_endpoint.py` (Slice 7.2)

`GET /v1/sessions/<session>/captures/<turn_id>` returns the
captured JSON verbatim. Errors:

- `404` if the file doesn't exist (turn never ran, never
  finished, or capture was off for this client). Body:
  `{"error": {"type": "not_found", "message": "...", "detail": "<reason>"}}`.
- `403` if bearer-auth fails (existing middleware).

`GET /v1/sessions/<session>/captures?limit=N&since=<ts>` (also
new) returns a list of recent turn ids with their summary
fields (alias, model_used, started_at, prompt_tokens,
finish_reason, narration_warning) but not the full bodies.
For dashboard listings.

**Path naming note.** Phase 4.8c already serves
`GET /v1/sessions/<id>/turns` for the per-event stream
(turn lifecycle events from `gateway.turns`). Slice 7.2's
captures sit at `/captures` to avoid collision — same
per-turn id space, different on-disk shape (sidecar JSON vs
JSONL events). Two distinct paths so the dashboard / CLI can
hit each independently.

### `dashboard/__init__.py` and friends (Slice 7.5)

A FastAPI sub-router mounted at `/dashboard`. Templates use
Jinja2; static assets (a small CSS file and the htmx.min.js
bundled at build time) live alongside.

Why HTMX over an SPA:

- No build step. The dashboard ships as Python + templates +
  three static files. Container image stays small.
- Server-rendered HTML is the natural shape for "show me a
  table of audit events." HTMX adds `hx-get` and
  `hx-trigger="every 5s"` on top — the live-update story for
  views that need it without a frontend test harness.
- Reuses the gateway's existing FastAPI / Pydantic /
  template-rendering muscle.
- Single-user scale doesn't need SPA reactivity. The live
  turns view is the only view that benefits from
  sub-second updates and SSE handles that without JS
  framework work.

Auth shape:

- Bearer token in `Authorization: Bearer <token>` works
  (same as the chat endpoint). For browser-based use, a
  small `/dashboard/login` page accepts the token, validates
  it against `secrets.allowed_tokens`, and issues a
  `dashboard_session` cookie signed with a key at
  `$FITT_HOME/dashboard.key` (generated on first use, 0600
  perms, same posture as `audit.key`).
- Cookie has 24-hour expiry. Logout deletes it.
- No CSRF protection in v0 — read-only surface, no state-
  changing endpoints. When edit support lands (follow-up),
  CSRF tokens at form-submit time become a real concern.

Page-by-page rendering:

- **Overview** — static-ish dashboard. Hits `/v1/aliases`,
  recent events count from `/v1/events`, MCP server status
  from `/v1/mcp`. Polls every 30s.
- **Aliases** — table over `/v1/aliases`. One row per
  alias. Polls every 60s.
- **Turns** — list view at `/dashboard/turns/<session>`,
  detail view at `/dashboard/turns/<session>/<turn_id>`. List
  hits `/v1/sessions/<s>/captures?limit=50`. Detail hits
  `/v1/sessions/<s>/captures/<turn_id>` and renders the captured
  detail in a structured form. For an active session, the
  list view SSE-subscribes to
  `/v1/sessions/<s>/turns/stream` (Phase 4.8c) and prepends
  new turns as they arrive.
- **Tools** — table over the existing `/v1/capabilities`
  endpoint plus per-tool last-invocation lookup from
  `audit.jsonl`. Polls every 30s.
- **Cron** — table over the existing `/v1/cron` (or whatever
  the existing endpoint is) plus recent firing events from
  `events.jsonl`. Polls every 60s.
- **Audit** — paged view over `/v1/audit`. Filter form for
  since / tool / session / decision. No live-update; this
  is a forensic view, not a live-debug view.
- **Health** — system status. Hits `/health` and `/ready`.
  Polls every 30s.
- **Gaps** — capability-gap log over `/v1/capability-gaps`.
  Static-ish; no live update.

### Telegram bot modules (Slices 7.3, 7.4)

`handlers.py` grows three new commands and extends one:

- `_on_lastturn` (new) — handles `/lastturn`. Reads
  `prefs.session_id`, calls
  `gateway.list_recent_turns(session_id, limit=1)`, then
  `gateway.get_turn(session_id, turn_id)`, formats and
  posts.
- `_on_status` (new) — handles `/status`. Calls a new
  `gateway.get_status()` that aggregates the existing
  `/health`, MCP status, cron status, pruner status.
- `_on_eval` (new) — handles `/eval <alias>`. Calls
  `gateway.run_eval(alias)` which proxies to the existing
  `alias_eval` harness via a new `POST /v1/eval/<alias>`
  endpoint. Long-running; the bot replies "running…" first,
  edits in the result.
- `handle_model_command` (extended; partial shipped
  2026-05-22). Now also pulls context-window data from
  `/v1/aliases` rather than `/v1/models` so the per-alias
  display includes the context window when known.

`gateway_client.py` grows:
- `list_aliases_full() -> list[AliasDetail]` — `/v1/aliases`
- `list_recent_turns(session, limit) -> list[TurnSummary]`
- `get_turn(session, turn_id) -> TurnCapture`
- `get_status() -> SystemStatus`
- `run_eval(alias) -> EvalReport`

`markdown_render.py` (new, Slice 7.4):

```python
def markdown_to_telegram_html(markdown: str) -> str:
    """Convert CommonMark to Telegram-compatible HTML.
    Whitelist-sanitised to <b>, <i>, <code>, <pre>, <a>,
    <blockquote>, <tg-spoiler>. Unsupported elements
    degrade to text content."""
```

Implementation:

1. Parse with `markdown_it.MarkdownIt("commonmark")`.
2. Walk the token stream. Map permitted tokens to their
   Telegram HTML equivalents; drop wrappers for unpermitted
   tokens (h1-h6, lists, tables) and emit just their text.
3. Escape `&`, `<`, `>` in non-tag text content per
   Telegram's HTML escape rules.

Applied at three call sites:

- `streaming.py::_flush` — the chat-streaming edit. Already
  has the accumulated text; converts before passing to
  `edit_message_text`.
- `turn_renderer.py::_flush_stream_bubble_if_due` — the
  growing bubble's flush. Same pattern.
- `handlers.py` — command response constructors that
  include user-visible model output (today minimal; the
  `/lastturn` response includes a result_summary that may
  contain markdown).

## Data shapes

### `/v1/aliases` response

See above.

### `/v1/sessions/<session>/captures/<turn_id>` response

Returns the `TurnCapture` JSON verbatim. Schema:

```json
{
  "turn_id": "uuid",
  "session_key": "main",
  "alias": "fitt-default",
  "client": "telegram",
  "model_used": "qwen2.5-coder:14b",
  "backend": "ollama",
  "fallback_used": false,
  "started_at": 1779479823.42,
  "finished_at": 1779479825.81,
  "dispatched_messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."}
  ],
  "response": { /* full LiteLLM response.model_dump() */ },
  "tool_calls": [
    {
      "call_id": "...",
      "tool_name": "read_file",
      "args": {"project": "fitt", "path": "README.md"},
      "decision": "auto",
      "decision_detail": "",
      "duration_ms": 12,
      "ok": true,
      "result_summary": "...",
      "artifact_path": null,
      "iteration": 0
    }
  ],
  "prompt_tokens": 5405,
  "completion_tokens": 103,
  "context_window": 32768,
  "prompt_pct_of_window": 16.5,
  "finish_reason": "stop",
  "narration_warning": false,
  "iterations": 1,
  "status": "ok"
}
```

### `/v1/sessions/<session>/captures?limit=N` response

Lightweight summary list:

```json
{
  "session_key": "main",
  "turns": [
    {
      "turn_id": "uuid",
      "started_at": 1779479823.42,
      "alias": "fitt-default",
      "model_used": "qwen2.5-coder:14b",
      "prompt_tokens": 5405,
      "context_window": 32768,
      "prompt_pct_of_window": 16.5,
      "finish_reason": "stop",
      "narration_warning": false,
      "tool_calls_count": 0,
      "status": "ok"
    }
  ]
}
```

### `/v1/eval/<alias>` request/response

```
POST /v1/eval/<alias>
  -> 200 with the EvalReport JSON

Body: {} (no parameters in v0; --realistic flag is a future
        kwarg)
```

The eval is synchronous from the bot's perspective — it
takes 30-60s and the bot waits. If we ever need async, switch
to a job-id-and-poll shape.

## Decisions with rationale

### D1. Discovery at boot, refresh on demand, no background poll

Decision: discover context windows once at boot, cache for
the lifetime of the process, expose `fitt context refresh`
for the manual-update case.

Considered: a background poll every N hours to pick up
remote model changes (Ollama operator changes
`OLLAMA_CONTEXT_LENGTH`, OpenRouter changes a model's
advertised context). Rejected — adds a daemon, adds
failure modes, and the operator who changed the upstream
config is the operator who knows to run `fitt context
refresh`. Discovery is not a long-running concern.

### D2. Per-turn JSON sidecar, not extended JSONL

Decision: turn bodies live in `<turn_id>.json` files;
events stay in `<YYYY-MM-DD>.jsonl`.

Considered: extend the JSONL with a `body` field on the
`turn_finished` event. Rejected — the events log is meant
to be cheap to tail (`fitt watch`, dashboard live view,
Telegram renderer); fat events full of 5K-token system
prompts make the tail expensive.

Considered: SQLite. Rejected — operationally heavier (db
file + lock semantics) for one-write-per-turn. Markdown +
JSON files match the rest of FITT's storage posture.

### D3. Capture default off for coding-agent clients

Decision: capture defaults to off for `coding-agent` client
tag (Aider, Claude Code, Cursor agent mode, etc.); on for
everything else. Operator can override via
`traceability.default_capture`.

Rationale: router-mode clients pass through the gateway
with their own system prompts that may contain user code,
secrets, tokens. The thin-router contract Phase 4 established
says FITT shouldn't inspect or persist their bodies.
Traceability for coding-agent flows is the agent's
responsibility, not FITT's.

### D4. `narration_warning` is a flag, not a signal

Decision: run `is_tool_use_expected_but_none` post-hoc as a
flag on `TurnCapture`; do not emit a `tool_call_narrated`
event in live chat.

This is the second time we've considered putting this in
live chat. The first attempt (2026-05-11 to 2026-05-12)
fired at 100% on chit-chat because the precondition (user's
message expected a tool call) is unknowable. We rolled it
back. See `docs/observed-issues.md`.

The flag-on-capture posture preserves the classifier's
diagnostic value (operator looking at a *specific* turn
sees the warning alongside other context) without recreating
the false-positive feedback loop. The classifier never
gates anything; it's annotation, not assertion.

### D5. Dashboard reads gateway endpoints, not files

Decision: every dashboard view consumes a gateway HTTP
endpoint, not the on-disk files directly.

Rationale: the dashboard's data path is the same path Raycast
widgets, third-party tools, and the future native-install
case follow. If the dashboard reads files, it inherits
deployment specifics (bind mount path, file format
versioning, race conditions on simultaneous read-write).
HTTP endpoint is the contract.

This forces some new endpoints to land in Slice 7.2 / 7.5
that we'd otherwise punt: `/v1/sessions/<s>/captures?limit=N`,
`/v1/eval/<alias>`, `/v1/status`. All small.

### D6. HTMX over SPA

Decision: server-rendered Jinja templates + HTMX for
interactivity. No Vue / React / Svelte / Solid.

Rationale: zero build step, fits FITT's Python-heavy
maintenance posture, the dashboard's interaction needs are
80% "show me a table" and 20% "edit-in-place." HTMX
handles both without a frontend test harness.

Cost: rich interactions (drag-drop config editor, complex
forms with client-side validation) are out of reach. The
read-only v0 doesn't need them; if edit support in the
follow-up demands them, escalate at that point.

### D7. Markdown to HTML, not MarkdownV2

Decision: convert CommonMark to Telegram HTML (whitelist-
sanitised), not to MarkdownV2.

Rationale: streaming edits commit partial messages mid-
flight. A half-written `<b>` is invalid HTML; Telegram
ignores broken HTML and renders the text. A half-written
`*…*` in MarkdownV2 crashes the parser for the whole
message. HTML's "fail soft" property is what makes it
viable for streaming.

### D8. Context discovery feeds compaction (Phase 8)

Decision: Slice 7.1 produces the data structure Phase 8
will consume. Specifically, `app.state.context_windows`
is the lookup Phase 8's compaction trigger reads from.

Rationale: compaction without a real ceiling is guessing.
The 95% threshold Claude Code uses is meaningless without
knowing what 100% is. By making context discovery a
prerequisite, Phase 8 picks up a real number.

This means Slice 7.1 must ship before Phase 8 starts.
Within Phase 7, 7.1 also unblocks the `/model` enrichment
(which already shipped in part) and the dashboard's
aliases view.

### D9. Reuse Phase 4.8c's SSE stream for live dashboard

Decision: dashboard's live turns view subscribes to the
existing `/v1/sessions/<s>/turns/stream` SSE endpoint,
the same one the Telegram renderer consumes.

Rationale: Hermes's documented architectural rule —
"don't reimplement the chat experience; reuse the event
stream." Same data flowing to both surfaces means a bug
in one surface's rendering is testable against the other,
and a new event kind on the gateway becomes
consumable by both surfaces simultaneously.

## Correctness properties

### P1. Discovery never blocks chat dispatch

Property: a slow or unresponsive backend during context
discovery cannot delay or fail a chat request.

Verification: discovery has its own timeout (5s default
per backend); failure stores `tokens=None`; the chat
handler reads context_window-or-None and proceeds.
Property test: simulate a 60s-hung Ollama probe; confirm
chat dispatches in <100ms regardless.

### P2. Capture failures never break chats

Property: an IO failure (full disk, unwritable path,
permission error) during sidecar capture cannot fail a
chat request or lose the response.

Verification: capture is a fire-and-forget asyncio task
launched after the response is sent. The chat handler
returns its response before awaiting capture. Property
test: monkeypatch the sidecar write to raise OSError;
confirm chat returns success.

### P3. Captured turns are byte-stable

Property: replaying a captured turn's `dispatched_messages`
through the same alias to the same model produces a
response with the same token counts (modulo backend
non-determinism).

Why: the trace claim "this is what the model saw" must be
literal. If the captured messages differ from what was
actually sent, traceability is lying.

Verification: integration test that captures a turn,
replays the captured messages through a fixture model,
asserts byte-equal request body. Skipped on CI (slow);
run in pre-release.

### P4. Markdown roundtrip-friendly under streaming

Property: any prefix of a CommonMark document, when
converted to Telegram HTML, produces valid HTML that
Telegram accepts.

Why: the streaming edit applies the converter to a
growing buffer. If a prefix produces invalid HTML, the
whole message edit fails.

Verification: hypothesis test. Generate CommonMark docs.
For every prefix from 1 char to full length, convert and
assert the result parses as HTML and conforms to
Telegram's tag whitelist.

### P5. Narration warning is annotation, not gate

Property: setting or clearing `narration_warning` on a
captured turn must never affect the chat response, the
agent loop's behaviour, or the user-facing message.

Verification: the chat handler computes the flag *after*
the response is built, never before. Code review
property; test by removing the flag computation and
confirming all behavioural tests still pass.

### P6. Privacy default for coding-agent

Property: a request from a `coding-agent` client without
explicit traceability config produces no
`<turn_id>.json` sidecar.

Why: the thin-router contract.

Verification: existing test infrastructure for
router-mode (`tests/e2e/test_router_mode.py` or similar)
extends with a "no sidecar written" assertion.

## Testing strategy

Per-slice test focus:

- **7.1**: per-backend discovery happy paths +
  failure modes (auth fail, network fail, malformed
  response). Cache lifecycle. `/v1/aliases` shape.
  ~15-20 unit tests.
- **7.2**: write path correctness (atomic, non-blocking),
  retention sweep, endpoint shapes, privacy default per
  client. ~15-20 unit + 2-3 integration tests.
- **7.3**: each command's happy path + missing-data path.
  Markdown rendering of `/lastturn` response. ~10-15 unit
  tests on the bot side.
- **7.4**: CommonMark conversion (every supported tag, every
  edge case Telegram cares about). Hypothesis property
  test for streaming-edit safety. ~20-30 tests.
- **7.5**: each dashboard view loads with a stub
  configuration; HTMX swap behaviour for live-update
  views; auth flow for cookie/login. Integration tests
  spinning up the gateway with the dashboard mounted.
  ~15-20 tests.

Global properties (across slices): see P1-P6 above.

## Cross-references

- Phase 4.8 spec
  (`.kiro/specs/phase4.8-visibility-proxies/`) — the
  per-turn event substrate. Phase 7 extends, doesn't
  replace.
- Phase 4.9 spec
  (`.kiro/specs/phase4.9-upstream-timeouts/`) — error
  classification reused by the dashboard's error displays.
- Phase 4.11 spec
  (`.kiro/specs/phase4.11-web-search/`) — web-search
  tool's reliance on context room is one of the consumers
  Phase 7's capacity awareness benefits.
- `docs/hallucinations-and-poisoning.md` — Problem D
  (invisibility) framing Phase 7 closes.
- `docs/observed-issues.md` — granite-narration entry
  (2026-05-22) is the inciting incident.
- `docs/choosing-a-model.md` — criterion 3 (system-prompt
  discipline) is the operator-facing knob Phase 7 makes
  diagnosable.
- `docs/prior-art.md` — OpenClaw and Hermes audits for
  dashboard view inspiration and the "don't reimplement
  the chat" architectural rule.

## Open questions

These are real open questions, not trick-by-omission. Land
in design.md so the spec captures the unknowns. Resolve as
implementation surfaces forces an answer.

1. **Should `/v1/aliases` be auth-exempt like `/v1/models`?**
   `/v1/models` is auth-exempt because OpenAI clients ping
   it before sending bearer tokens. `/v1/aliases` is FITT-
   internal and doesn't need that compatibility. Default:
   gated.

2. **How does the dashboard know what session to default
   to?** The `?session=main` query param is the obvious
   answer. For a multi-session operator, the overview page
   could list active sessions and the per-view defaults
   sticky-cookie to the last-viewed. Solve when the
   first multi-session operator shows up.

3. **What's the `narration_warning` UX in the dashboard?**
   A row badge plus a tooltip explaining what it means is
   the v0 answer. If false-positive rate looks meaningful,
   we may want a "dismiss this annotation" mechanism.
   Defer until live use produces feedback.

4. **Is `/v1/eval/<alias>` POST or GET?** POST because the
   eval has side effects (writes a report file), even
   though semantically it's a "compute and return" call.
   REST purists would object; POST is the safer default.

5. **Where does the markdown renderer live?** Bot-side
   (Slice 7.4 plan) keeps the gateway free of the
   conversion. Alternative: gateway pre-renders to HTML
   for telegram clients only, identified by client tag.
   Bot-side wins on isolation and on keeping the gateway
   markdown-output-format-agnostic.

6. **How does the dashboard handle very long captured
   turns?** A 5K-token system prompt + 100-message
   history + 10 tool calls is several hundred KB of JSON.
   Render strategy: collapse-by-default, expand-on-click
   per section. Pagination if a single section is too
   large.

7. **Does the per-turn capture include MCP tool calls?**
   They go through the same `tool_calls_for_memory`
   pipeline, so yes by default. Confirm in implementation.

8. **What happens to capture on a turn that detaches?**
   Phase 4.5's detached-execution path returns a
   placeholder before the tool loop completes. Capture
   should still run when the loop eventually finishes;
   the `turn_finished` event already fires. Verify the
   capture path reads the *eventually-final* response,
   not the placeholder.

These are the questions worth pinning before coding starts.
Implementation may surface more; treat any new "unknown" as
a design.md update before merging.
