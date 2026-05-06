# Phase 4.5 — Cron + Proactive Notifications: Design

## Overview

Two primitives:

- **Cron**: a scheduled asyncio timer fires a fresh agent session
  with a configured prompt on its schedule.
- **Events**: append-only log of notable async activity, pushed to
  Telegram by default.

Together they unlock "monitor X, tell me when done" without the
user being in an active conversation.

## Architecture

```
                                          +---------- Gateway process --------+
                                          |                                   |
   $FITT_HOME/cron.json  <--- fcntl-locked+ CronService                       |
   (atomic writes)                        |   timer loop                      |
                                          |   fires per-cron asyncio tasks    |
                                          |       |                           |
                                          |       v                           |
                                          |   spawn fresh session             |
                                          |   with cron.message as prompt     |
                                          |       |                           |
                                          |       v                           |
                                          |   run agent loop                  |
                                          |   (tools, approval, audit)        |
                                          |       |                           |
                                          |       v                           |
                                          |   emit events per step            |
                                          |       |                           |
                                          |       v                           |
                                          |   (unless silent)                 |
                                          |   auto-deliver final reply        |
                                          |                                   |
   $FITT_HOME/events.jsonl <-- EventLog   |   append each event               |
                                          |       |                           |
                                          |       v                           |
                                          |   TelegramPush                    |
                                          |       |                           |
                                          +-------+---------------------------+
                                                  |
                                                  v (outbound only)
                                          api.telegram.org -> your phone
```

Three principles drive the design.

1. **Crons are sessions.** A cron firing spawns an agent session
   (identity + memory + tools) and runs the cron's message as its
   prompt. No new subsystem for "cron execution"; reuse the
   existing session + tool + approval machinery.
2. **Events are delivery-independent.** Every event lands in
   `events.jsonl`. Delivery to Telegram is a separate concern
   (a subscriber of the event stream). The log is the source of
   truth; the push is how the user finds out.
3. **Silent and approval-mode are cron-level overrides on the
   Phase 4 policy stack.** When a cron fires, its `approval_mode`
   and `silent` flags layer on top of per-tool / per-client
   policy. No new bucket; just an additional layer.

## Three logs, three jobs

FITT now has (or will have) three append-only logs. Worth
pinning the boundaries so we don't accidentally merge them
during implementation.

| Log | Path | Purpose | Durability |
|---|---|---|---|
| Audit (Phase 4) | `audit.jsonl` | Every tool call with before/after context. Security-relevant, tamper-evident. | HMAC-chained. One entry per tool attempt including rejections. |
| Capability gaps (Phase 4) | `capability_gaps.log` | "I'd need a tool to X" model complaints, ranked. Feeds the next-tool backlog. | Plain JSONL. One entry per parsed gap phrase. |
| Events (Phase 4.5) | `events.jsonl` | User-visible async activity (cron fired / completed / failed, agent_message, late_tool_result, approval_requested). Feeds Telegram push and `fitt inbox`. | Plain JSONL. Coarse-grained. Pruned after N days. |

They overlap in places — a tool call that was approved late is
both auditable (audit.jsonl) and user-visible (events.jsonl).
Keeping them separate keeps each file's semantics tight: audit
is exhaustive and secure, gaps are a focused backlog, events
are what the user would scroll through in Telegram. Merging
earns its place only when a query spans all three and the
separation becomes a chore.

## Data model

### CronJob

```python
@dataclass
class CronSchedule:
    kind: Literal["every", "at", "cron"]
    every_secs: int | None = None       # kind=every
    at_ts: float | None = None          # kind=at (unix epoch)
    cron_expr: str | None = None        # kind=cron (5-field)


@dataclass
class CronJob:
    id: str                             # 8-char hex, ~50 bits
    name: str                           # human-readable
    message: str                        # prompt sent to the agent on fire
    schedule: CronSchedule
    enabled: bool = True
    silent: bool = False                # suppress auto-delivery of replies
    approval_mode: Literal["", "auto"] = ""  # "" = inherit; "auto" = auto-approve inside the cron
    agent_alias: str = ""               # which FITT alias the cron uses; empty = default
    session_key: str = ""               # who created it (for scoped removal)
    created_by_client: str = ""         # ide / telegram / cli / webui
    created_ts: float = 0.0
    last_run_ts: float | None = None
    last_status: Literal["", "ok", "error"] = ""
    last_error: str = ""
    delete_after_run: bool = False      # for one-shot at-style jobs
```

Persistence: `$FITT_HOME/cron.json`. Atomic writes (tmp file +
rename). `fcntl.flock` during read-modify-write cycles so the CLI
and gateway don't corrupt the file.

Mtime-based external-change detection: timer loop checks the file
mtime every 30s; if changed externally (CLI edit), reload.

### EventEntry

```python
@dataclass
class EventEntry:
    ts: float                           # unix epoch
    kind: str                           # "cron_fired" / "cron_completed" / "approval_requested" / "task_completed" / "agent_message" / ...
    session_key: str                    # session the event belongs to
    title: str                          # short human-readable
    body: str                           # longer content or summary (may be empty)
    meta: dict                          # kind-specific metadata (cron_id, tool_name, ...)
```

Persistence: `$FITT_HOME/events.jsonl`. Append-only, one JSON
object per line. No locking needed (append is atomic at the OS
level for writes below PIPE_BUF size; we flush per write).

Pruning: nightly task deletes entries older than
`events.max_age_days` (default 90). Implementation: streams the
file, writes kept entries to a tmp file, renames.

## Module design

### `gateway/cron.py`

```python
class CronService:
    def __init__(
        self, base_dir: Path,
        on_fire: Callable[[CronJob], Awaitable[None]],
    ) -> None: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    # Mutations (file-locked)
    def add(self, job: CronJob) -> CronJob: ...
    def update(self, id: str, **fields) -> CronJob | None: ...
    def remove(self, id: str) -> bool: ...
    def enable(self, id: str, enabled: bool) -> bool: ...

    # Queries
    def get(self, id: str) -> CronJob | None: ...
    def list(self, include_disabled: bool = False) -> list[CronJob]: ...

    # Internals
    async def _timer_loop(self) -> None: ...
    def _is_due(self, job: CronJob, now: float) -> bool: ...
    def _next_run(self, job: CronJob, now: float) -> float | None: ...
    async def _fire(self, job: CronJob) -> None: ...
```

`on_fire` callback handed in by the gateway on startup. It's
responsible for spawning a fresh session and running the cron's
message through the agent loop.

Timer loop: wakes on the next-due job's schedule or after 30s
(whichever is sooner), checks all enabled jobs, fires any due.
Each firing runs as an independent asyncio task so one hung job
doesn't block others. Per-firing timeout from
`cron.timeout_secs` (default 30 min; configurable).

### Cron tools (inline)

Registered in `gateway/tools/cron_tools.py`:

- `cron_add(name, message, schedule_spec, silent=false, approval_mode="", agent_alias="", timezone="")` → `{id, name, schedule}`
- `cron_list()` → formatted string with all jobs + next run times
- `cron_update(id, **fields)` → confirmation
- `cron_remove(id)` → confirmation
- `cron_pause(id)` / `cron_resume(id)` → confirmation

`schedule_spec` accepts one of: `"every <n>s|m|h"`, `"at <iso>"`,
`"cron <expr>"`, `"in <n> minutes"`. Parser resolves to a
`CronSchedule`.

`agent_alias == ""` means "use the gateway's default alias"
(resolved at fire time from the gateway's `aliases.fitt-default`
mapping in `config.yaml`, matching what an unqualified chat
request would route to). Resolving at fire time (not at add
time) means a cron created before the user switches their
default alias automatically picks up the new default.

Default approval buckets:
- `cron_add`: `ask` (creation requires human approval).
- `cron_list`, `cron_pause`, `cron_resume`: `auto`.
- `cron_update`, `cron_remove`: `ask`.

### `gateway/events.py`

```python
class EventLog:
    def __init__(self, path: Path) -> None: ...

    def append(self, entry: EventEntry) -> None: ...   # atomic single write
    def read(self,
             since: float | None = None,
             kind: str | None = None,
             session: str | None = None,
             limit: int | None = None) -> Iterable[EventEntry]: ...

    def prune(self, max_age_days: int) -> int: ...
```

### `gateway/telegram_push.py`

New helper (or an addition to the existing telegram-bot package;
the bot still owns Telegram API access).

```python
class TelegramPusher:
    def __init__(self, bot_client) -> None: ...

    async def push(self, entry: EventEntry) -> None:
        # Deliver the entry as a Telegram message to the single allowlisted user.
        # Format depends on entry.kind.
```

Wiring: when the EventLog receives an entry, it calls
`pusher.push(entry)` asynchronously (fire-and-forget). Delivery
failures are logged but don't block the producer.

### `send_message` inline tool

Registered in `gateway/tools/send_message.py`:

```python
@tool(
    name="send_message",
    description="Send a proactive message to the user. Use when you want to notify them of something outside a normal chat turn.",
    schema={...},
    bucket=ApprovalBucket.AUTO,
)
async def send_message(args, context) -> ToolResult:
    text = args["text"]
    title = args.get("title", "")
    # Rate limit
    if context.send_message_budget_exceeded():
        return ToolResult.error("rate limit exceeded")
    entry = EventEntry(
        ts=time.time(),
        kind="agent_message",
        session_key=context.session,
        title=title or "Agent Message",
        body=text,
        meta={"tool": "send_message"},
    )
    context.events.append(entry)
    # Telegram push is automatic via the event log subscriber
    return ToolResult.ok("sent")
```

Rate limit: in-memory counter per session, window `send_message.window_secs` (default 60), ceiling `send_message.max_per_window` (default 10).

### CLI

`fitt cron`:
- `fitt cron list [--all]` — pretty-print active (and paused) jobs.
- `fitt cron add --name X --every 60 --message "..."` — create a
  cron from the CLI (bypasses approval; CLI user is the human).
- `fitt cron remove <id>`, `fitt cron pause <id>`, `fitt cron resume <id>`, `fitt cron run <id>` (fire once now).

`fitt inbox`:
- `fitt inbox [--since 24h|7d|...] [--kind cron_*] [--session name] [--limit N] [--json]`.
- Default: last 24h, all kinds, current session (`main`).
- Pretty-printed by default; `--json` for programmatic use.

### Integration with Phase 4 approval

When a cron fires:

1. Timer loop identifies the due cron.
2. Spawns an agent session with `session_key = "cron:{id}"`,
   identity + memory injected, `agent_alias = cron.agent_alias`.
3. The cron's `message` is submitted as the user prompt.
4. Model decides on tool calls. Each goes through the Phase 4
   approval middleware with one extra layer:
   - If the cron has `approval_mode == "auto"`, the decision for
     any `ask`-bucket tool becomes `auto` inside this session.
   - If `approval_mode == ""`, the normal user-default applies
     (but routed to Telegram because the cron client is
     effectively headless).
5. After tool loops complete, the model produces a final reply.
6. If `silent == false`: emit an `EventEntry(kind="cron_completed")`
   with the reply as body. Telegram push delivers it.
7. If `silent == true`: no auto-emit. The only way the user hears
   from this cron is if the agent explicitly called
   `send_message`.
8. Emit an `EventEntry(kind="cron_fired")` regardless for the log.

## Detached delivery (late-approved tools)

Closes the Phase 4 rough edge: tool approved after the bot's
HTTP client has timed out (~45s cap today). Without this, the
tool runs, succeeds, and its result vanishes — no Telegram
message, no visible trail except the audit log.

**Rule of thumb.** If a chat turn's approval resolves *after*
the HTTP response has already been returned to the client, the
remaining tool-loop work continues on the gateway side and its
output is delivered via the push channel instead of the now-
closed request.

Data flow:

1. Chat handler requests approval. The 45s timeout fires before
   the user taps. Handler catches the timeout, flips the
   pending approval record to `detached=True`, and returns a
   placeholder response to the client:
   `"⏳ Approval pending — I'll message you when this completes."`
2. User eventually taps approve (or the prompt expires, or the
   user taps reject). The approval middleware resolves the
   future as usual.
3. A background coroutine that was spawned at detach-time is
   waiting on that future. It wakes, completes the remaining
   tool-loop iterations in the same session, and emits a
   `late_tool_result` event when done (or `late_tool_rejected`
   if the user rejected).
4. TelegramPusher formats the event as a new message, threaded
   to the original session via `session_key`.

Key invariants:

- **No silent drops.** Either the synchronous path completes
  in time, or the detached path pushes an event. Every chat
  turn with an `ask`-bucket tool produces exactly one user-
  visible response.
- **One detached worker per pending approval.** When we flip to
  `detached=True`, the approval middleware owns the lifecycle;
  the chat handler is done. If the gateway restarts while a
  detached worker is running, the tool result is lost (documented
  limitation). `cron.json`-style persistence for detached
  approvals is a Phase 7+ hardening item.
- **Session memory continues to update.** The detached worker
  uses the same `MemoryStore.append_turn` call path as the
  synchronous handler, so "what did we talk about earlier" keeps
  working when you scroll back in Telegram.
- **No bot changes for delivery.** The bot already consumes
  events via the normal push pipeline (section above); a
  `late_tool_result` event is just another event kind.

Config: `tools.approval_detach_threshold_secs` (default: equal to
`tools.approval_timeout_secs`). When approval wait exceeds this,
detach. Setting it above `approval_timeout_secs` disables
detach; set equal (default) to detach exactly when the bot's
HTTP call would have timed out.

## Event kinds

Standard taxonomy (extensible per phase):

- `cron_fired` — a cron just started running (before agent loop).
- `cron_completed` — cron agent loop finished successfully. Body
  is the agent's final reply (unless silent).
- `cron_failed` — cron errored. Body is the error.
- `approval_requested` — a tool call reached an `ask` state and
  is waiting for user decision.
- `approval_resolved` — user decided (approved / rejected /
  trust_session). Added regardless of the outcome for trail
  purposes.
- `late_tool_result` — a tool call that was approved after its
  chat turn had already detached. Body is the tool result; meta
  includes `original_session_key`, `tool`, and `approval_id`.
- `late_tool_rejected` — user rejected after detach; the chat
  turn's placeholder response is followed up with this event so
  the user knows the tool did not run.
- `agent_message` — explicit `send_message` tool call. Body is
  the text.
- `task_started` / `task_completed` / `task_failed` — reserved
  for Phase 6 (task runner).

(Capability gaps stay in their own log — see "Three logs, three
jobs" above.)

## Configuration additions

### `config.yaml`

```yaml
cron:
  # Per-firing timeout; firing that exceeds this aborts with an error event.
  timeout_secs: 1800                    # 30 min
  # Poll interval for the timer loop.
  poll_interval_secs: 30
  # Minimum interval-kind schedule.
  min_interval_secs: 60

tools:
  # Phase 4.5: when an ask-bucket approval is still pending after
  # this many seconds, the chat handler detaches. The remaining
  # tool-loop work continues in the background and delivers its
  # result as a push event. Default: mirror approval_timeout_secs.
  approval_detach_threshold_secs: 45

events:
  max_age_days: 90
  # When pushing to Telegram, cap the body length (longer is truncated with a note).
  telegram_body_cap: 3500

send_message:
  window_secs: 60
  max_per_window: 10
```

### `secrets.yaml`

No changes needed; Telegram bot token is already there.

## Tests

### Unit

- `test_cron_schedule.py`: `CronSchedule.next_run` for all three
  kinds, across DST boundaries (cron expressions), across minute
  boundaries (every).
- `test_cron_service.py`: add / update / remove / enable,
  persistence and atomic-write behavior, external-change
  reload via mtime, file lock contention.
- `test_cron_tools.py`: each tool in isolation (validation,
  success, error messages).
- `test_events.py`: append, read with filters, pruning.
- `test_send_message.py`: rate limit triggers, event emission,
  Telegram push is called.
- `test_telegram_push.py`: each event kind formats to a
  reasonable Telegram message; body cap is honored.

### Property-based

- **Cron next_run monotonicity**: for a given cron expression,
  `next_run` is strictly increasing across consecutive calls.
- **Event ordering**: entries appended in order are read back in
  order.

### Integration

- End-to-end: create a cron via the `cron_add` tool, wait one
  firing (with a 1-second cron for test), verify agent session
  was spawned and completed, verify event in log, verify
  Telegram push call.
- Silent mode: same flow but `silent=true`; no auto-delivery;
  verify `send_message` path works.
- Approval mode: same flow but with `approval_mode="auto"`;
  verify a tool call that would normally be `ask` runs without
  prompting.

## Rollout

Implementation order to keep the tree green:

1. `gateway/events.py` with tests. No producers yet.
2. `gateway/cron.py` data model, persistence, tools (without
   scheduler firing yet — just CRUD). Unit tests.
3. Scheduler loop that fires due jobs. Test with a 1-second cron
   firing a no-op callback.
4. Wire cron firing to an agent-session callback. Fresh sessions
   spawn, run the message, emit events.
5. `send_message` inline tool.
6. Telegram push subscriber on the event log.
7. `fitt cron` and `fitt inbox` CLIs.
8. Integration tests.
9. Docs.
10. Live validation.

## Open design decisions

1. **Cron persistence format.** JSON vs SQLite. JSON is simpler
   and human-readable (you can open `cron.json` in a text editor
   to see your crons, which is part of the "shareable / inspectable"
   vibe). SQLite would handle the "multiple processes poking at
   it" case more gracefully. v0 sticks with JSON + fcntl; revisit
   if contention becomes an issue.

2. **Per-firing vs shared session.** Each firing gets a fresh
   session (`cron:{id}:{timestamp}`). Alternative: persistent
   per-cron session that keeps context across firings. v0 picks
   fresh-per-firing: simpler, no context bloat, matches the
   "briefing" use case (stateless). Persistent sessions can be
   added as a cron flag later if needed.

3. **Event log vs audit log.** Phase 4 already ships an audit log
   for tool calls. Phase 4.5's event log is for user-visible
   async activity. They overlap in places (a tool call that was
   approved and executed is both auditable and maybe user-visible).
   Decision: keep them separate. Audit captures every tool call
   (fine-grained, HMAC-chained, security-relevant). Events capture
   user-visible moments (coarse, no HMAC, UX-relevant). A future
   phase could merge them with different views over a unified
   stream.

4. **`send_message` on non-Telegram.** The tool currently
   implicitly targets Telegram. If the user is not on Telegram but
   only uses Open WebUI, where does the message go? For v0:
   accept that Telegram is the push surface. If there's no
   Telegram configured, `send_message` becomes a no-op with a
   warning in the log. Phase 7+ (admin UI) could add a browser
   notification channel.

5. **Time zone handling.** Cron expressions are UTC by default.
   Each cron can override with a `timezone:` field (IANA name).
   Config-level default timezone lives in `config.yaml` under
   `server.timezone`. DST transitions handled by `croniter` /
   `zoneinfo` stdlib. v0 accepts that "9 AM" is ambiguous unless
   the user says "9 AM America/Los_Angeles."
