# Phase 4.5 — Cron + Proactive Notifications: Requirements

## Background

Phase 4 gives FITT tools. Requests are still strictly reactive: you
send a message, the agent acts, a response comes back. That works
for "read the repo" but not for "monitor the training run, tell me
when it's done." The latter needs FITT to do something without a
user request in flight, and to emit a message the user didn't ask
for.

Phase 4.5 adds the two primitives that unlock that use case:

- **Cron**: scheduled agent sessions that fire on their own.
- **Events**: an append-only log of async activity, with each event
  pushed to Telegram by default.

Both are small primitives. Their combination unlocks the
"monitor X and tell me when done" pattern everyone wants from a
personal AI.

## Why now

Phase 4 is the minimum viable tool layer. With it, synchronous
agent work is useful. Phase 4.5 makes the asynchronous use case
viable: set a schedule, walk away, get pinged when something
notable happens.

A sequencing note: without Phase 5 (lessons), each new monitoring
request requires re-explaining the setup. With Phase 4.5 alone,
"monitor training pid 1234" works the first time but "monitor
training pid 456" the next day also requires re-explanation
because FITT has no memory of the pattern. Phase 5 closes that
gap. Phase 4.5 is the necessary plumbing.

## Goals

1. **Scheduled agent sessions.** A cron job fires on its schedule,
   spawns a fresh session with a configured prompt, lets the
   agent do its thing, and cleans up.
2. **Three schedule kinds.** `every <seconds>` (interval), `at
   <timestamp>` (one-shot), `cron <5-field expr>` (classic
   cron-expression recurring schedule).
3. **Per-cron policy.** Each cron can set its own `approval_mode`
   (auto vs user default) and `silent` flag (suppress
   auto-delivery of results; agent decides via `send_message`).
4. **Cron tools.** `cron_add`, `cron_list`, `cron_update`,
   `cron_remove`, `cron_pause`, `cron_resume`. Available to the
   agent; approval applies per normal policy (`cron_add` in `ask`
   bucket by default).
5. **Event log.** Append-only `$FITT_HOME/events.jsonl`. Every
   async event (cron fired, tool call needing approval, long-
   running task completed) emits one entry.
6. **Proactive Telegram delivery.** Every event pushed to Telegram
   by default. `silent` mode suppresses auto-delivery; the agent
   emits what it wants explicitly.
7. **`send_message` tool.** Lets the agent emit a custom Telegram
   message (and corresponding event) when it decides the user
   should be notified.
8. **Inbox CLI.** `fitt inbox` reads the event log with filters.
   No web UI in this phase.

## Non-goals

- **Heartbeat loop** (self-directed agent behavior, picking up
  self-written todos every 60s). Deferred.
- **Subagents / parallel background work.** Deferred.
- **Web dashboard for events.** Deferred; Telegram + `fitt inbox`
  is enough.
- **Counters, filters, search, read/unread state** in the event
  log. Flat append-only file for now.
- **Cross-device notification sync.** Telegram-only delivery.
- **Notification deduplication / rate-limiting.** Rely on cron
  `silent: true` + agent judgement for polling-style crons.

## User stories

### U1 — Schedule something simple

> As a Telegram user, I want to say "every weekday at 8 AM, give
> me a briefing of open PRs," and then have FITT message me at
> 8 AM every weekday.

Acceptance:
- Agent translates the request into a `cron_add` tool call with
  `cron: "0 8 * * 1-5"` and a message describing the briefing.
- Approval prompt appears in Telegram. User approves.
- At 8 AM the next weekday (and every weekday after), the cron
  fires: a fresh session runs the briefing prompt, the model
  replies, the reply is delivered to Telegram.
- The event log records the firing and the outcome.

### U2 — Monitor until done (silent polling)

> As a Telegram user, I want to say "monitor the training job at
> `/home/fred/runs/1234/status.json`, tell me when it finishes,"
> and have FITT check every minute silently and ping me only
> when the state changes to done or failed.

Acceptance:
- Agent calls `cron_add` with:
  - `every: 60` (seconds)
  - `silent: true`
  - `approval_mode: "auto"` (tools within the cron auto-approve)
  - Message: "read the status file, parse, send_message if done or
     failed, otherwise say nothing."
- Initial approval prompt in Telegram. User approves once.
- Cron fires every 60s. Each firing reads the file, decides not
  to notify (state still running). Silent.
- When the file says "completed", the agent calls `send_message`
  with the final metrics. Telegram message arrives.
- The event log shows each firing plus the final `send_message`.

### U3 — List and manage crons

> As a Telegram or CLI user, I want to see my active crons and
> remove the ones I don't need anymore.

Acceptance:
- `cron_list` tool callable from chat. Model responds with a
  formatted list.
- `fitt cron list` CLI produces the same list formatted for
  terminal.
- `cron_remove`, `cron_pause`, `cron_resume` work. Changes
  persist across gateway restarts.

### U4 — Inbox from CLI

> As a user, I want to see the last 24 hours of events with
> filters, so I can catch up after being away.

Acceptance:
- `fitt inbox [--since <spec>] [--kind <kind>] [--session <name>]`.
  Reads `events.jsonl`, filters, prints with timestamps.
- Default `--since` is 24h. Default no filters beyond `--since`.
- Output is human-readable; `--json` option produces structured
  output for further processing.

### U5 — Cron survives restart

> As a user, I want my crons to keep firing after a gateway
> restart or NAS reboot.

Acceptance:
- Cron state persisted to `$FITT_HOME/cron.json` atomically on
  every mutation.
- On gateway startup: load `cron.json`, restart the scheduler
  timer for each enabled cron, log how many were restored.
- Tested with a gateway restart during a live cron.

### U6 — Agent-emitted messages

> As a user, I want the agent to message me proactively when it
> judges something is worth telling me.

Acceptance:
- `send_message(text, title?)` tool registered. Available to all
  sessions (chat, cron, task-runner once Phase 6 ships).
- Default bucket `auto` (low-risk; recipient is the single
  allowlisted user).
- Emits an event with `kind: "agent_message"` and delivers to
  Telegram.
- Distinct from the normal chat reply path (chat replies go
  through the existing streaming response; `send_message` is
  fire-and-forget and used when the agent isn't currently in a
  user-facing turn).

## Scope boundaries

**In scope:**

- `gateway/cron.py` — cron subsystem (schedule types, timer loop,
  persistence, file lock, mtime sync).
- Cron MCP-style inline tools: `cron_add`, `cron_list`,
  `cron_update`, `cron_remove`, `cron_pause`, `cron_resume`.
- Per-cron policy fields: `silent`, `approval_mode`,
  `session_key`, `agent_alias`.
- `gateway/events.py` — event log with append and read API.
- `send_message` inline tool.
- Chat handler changes: after each tool call or response,
  the event logger writes an entry if warranted.
- Telegram bot: handle incoming async messages from the gateway
  (push messages initiated by the agent, not reactive to user
  input).
- `fitt cron` CLI (list, add, remove, pause, resume).
- `fitt inbox` CLI.

**Out of scope:**

- Heartbeat loop (self-directed behavior).
- Subagents.
- Web UI for events or crons.
- Event deduplication.
- Parallel cron execution beyond a configurable ceiling
  (accept the OS-level asyncio default).

## Risks and open questions

### R1 — Approval fatigue on the first cron

**Risk:** First-time users create a cron, get spammed with
approval prompts on every firing because they didn't know to set
`approval_mode: auto`.

**Decision:** document that crons you expect to run unattended
need `approval_mode: auto`. The agent (via system prompt
guidance) should propose `approval_mode: auto` by default when
creating crons, unless the user specifically asks for per-firing
approval.

### R2 — Spam if `silent` isn't set for polling

**Risk:** A "monitor every 60s" cron without `silent: true`
produces 60 Telegram messages per hour.

**Decision:** same as R1. The agent proposes `silent: true` by
default for interval crons below a threshold (say, 10 minutes).
The user sees the proposal in the approval message and can
override.

### R3 — Timer drift

**Risk:** The asyncio timer loop drifts if the event loop is busy.
A cron scheduled for 09:00:00 might fire at 09:00:07.

**Decision:** accept for v0. This isn't a financial system. 10
seconds of drift in a minute-resolution scheduler is irrelevant.

### R4 — Gateway restart during a cron fire

**Risk:** Cron fires at 08:00:00. Gateway restarts at 08:00:05
before the session completes. The cron's work is lost.

**Decision:** accept. The next firing (e.g. next day for a daily
cron) just runs. For `at` one-shots, the user sees the cron got
missed and can re-fire manually.

### R5 — Event log volume

**Risk:** Event log grows unbounded.

**Decision:** nightly pruning job deletes entries older than 90
days (configurable). Writes a "pruned N entries" event to the
start of the new log chunk so there's a record.

### R6 — `send_message` abuse

**Risk:** The agent in a loop calls `send_message` 100 times per
minute.

**Decision:** rate-limit `send_message` to a configurable ceiling
(default 10 per minute per session). Excess calls return an error
to the model; next call succeeds after the window.

### R7 — Agent doesn't know about active crons

**Risk:** User asks "what are you monitoring for me?" The agent
doesn't know unless someone told it.

**Decision:** `cron_list` is auto-approved. The agent can call it
when asked. A summary of active crons ("you have 3 active
scheduled jobs") is optionally included in the system prompt;
configurable.

## Success criteria

Phase 4.5 is done when:

1. The RL-monitoring use case works end-to-end:
   - User says "monitor training file X, ping me when done."
   - Agent proposes a cron. User approves once.
   - Cron fires silently, eventually sends one notification, then
     (if written to do so) removes itself.
2. Crons survive gateway restart.
3. `fitt inbox --since 1d` shows yesterday's activity.
4. A daily briefing cron (`cron: "0 8 * * *"`) runs on schedule
   for 3 consecutive days.
5. All existing tests pass.
6. Author has lived with a real cron for 1 week without wanting
   to revert.
