# Phase 4.5 — Cron + Proactive Notifications: Tasks

Status legend: `[x]` done, `[ ]` not yet.

## 1. Event log primitive

- [x] 1a. `gateway/events.py`: `EventEntry` dataclass, `EventLog`
       with `append`, `read` (with `since` / `kind` /
       `session` / `limit` filters), `prune` (delete old entries).
- [x] 1b. Persistence at `$FITT_HOME/events.jsonl`. One JSON
       object per line. Append uses `open(..., 'a')`, flush after
       each write.
- [x] 1c. Tests: round-trip, filters, pruning (streamed rewrite).

## 2. Cron data model + persistence

- [x] 2a. `gateway/cron.py`: `CronSchedule`, `CronJob` dataclasses.
- [x] 2b. `CronService` CRUD: `add`, `update`, `remove`, `enable`,
       `get`, `list`. Persistence to `$FITT_HOME/cron.json` with
       atomic writes + fcntl lock.
- [x] 2c. Mtime-based sync: reload on external file change.
- [x] 2d. Schedule-string parser: `"every 60s"`, `"every 5m"`,
       `"at 2026-05-01T09:00"`, `"in 30 minutes"`, `"cron 0 9 * * *"`.
       Returns a `CronSchedule`.
- [x] 2e. `next_run_ts(job, now)` computation for all three
       schedule kinds.
- [x] 2f. Tests: CRUD, persistence, schedule parsing, next-run
       logic.

## 3. Cron inline tools

- [x] 3a. `cron_add`, `cron_list`, `cron_update`, `cron_remove`,
       `cron_pause`, `cron_resume`. Each calls the corresponding
       `CronService` method.
- [x] 3b. Register with the Phase 4 tool registry. Default buckets:
       `cron_list` / `cron_pause` / `cron_resume` = `auto`;
       `cron_add` / `cron_update` / `cron_remove` = `ask`.
- [x] 3c. Tests.

## 4. Scheduler loop

- [x] 4a. `CronService.start()` starts an asyncio timer loop.
       Every 30s (or sooner if a job is due): scan `list()`, fire
       due jobs as independent asyncio tasks.
- [x] 4b. `_is_due(job, now)` for each schedule kind.
- [x] 4c. Fired jobs run under `asyncio.wait_for(..., timeout_secs)`.
       Timeout → `last_status="error"`, `last_error="timeout"`.
- [x] 4d. `_fire(job)` calls the registered `on_fire(job)` callback.
- [x] 4e. Tests with a 1-second interval and a no-op callback.

## 5. Cron firing → agent session integration

- [x] 5a. Register `on_fire` callback in the gateway startup that
       spawns a fresh session (`session_key = "cron:{id}:{ts}"`)
       and runs `job.message` through the agent loop.
- [x] 5b. Apply `job.approval_mode` as an override on the approval
       middleware for this session.
- [x] 5c. If `silent == False`, emit `cron_completed` event with
       the agent's final reply as body.
- [x] 5d. If `silent == True`, emit `cron_completed` with an
       empty body.
- [x] 5e. Always emit `cron_fired` at the start.
- [x] 5f. On error: emit `cron_failed` with the traceback
       summary.
- [x] 5g. Integration test: create a cron, wait a firing,
       verify session spawned + events emitted.

## 5.5 Detached delivery (closes Phase 4 approval-timeout rough edge)

- [x] 5.5a. Config field `tools.approval_detach_threshold_secs`
        (default: equals `approval_timeout_secs`). Parsed into
        the same `ToolPolicy` block.
- [x] 5.5b. Chat handler: when an approval is still pending at
        the detach threshold, flip the pending-approval record
        to `detached=True` and return a placeholder response
        (`"⏳ Approval pending — I'll message you when this
        completes."`). Kicks off a detached worker coroutine
        that awaits the same future.
- [x] 5.5c. Detached worker: on approval resolve, complete the
        remaining tool-loop iterations in the same session,
        then emit `late_tool_result` (on approve) or
        `late_tool_rejected` (on reject). Memory append runs
        as usual.
- [x] 5.5d. `cron_add` / `send_message`-style warning: if no
        push channel is configured (no Telegram bot running,
        no other subscribers), tool results that detach fall
        back to the event log only. Log a clear WARNING at
        detach time.
- [x] 5.5e. Telegram formatter for `late_tool_result` /
        `late_tool_rejected` (threaded to original session).
- [x] 5.5f. Tests: full detach lifecycle with stubbed LLM +
        stubbed approval future — assert the placeholder lands
        synchronously, the event lands asynchronously, and the
        session memory contains both halves.

## 6. `send_message` tool

- [x] 6a. Inline tool with bucket `auto`.
- [x] 6b. Per-session rate limiter. On excess, return a
       structured error with `retry_after_secs` in the payload
       so the model can back off. Configurable window /
       ceiling via `send_message.window_secs` + `max_per_window`.
- [x] 6c. Emits `agent_message` event on success.
- [x] 6d. When no push channel is configured, log a WARNING
       once per gateway process (not per call) and still emit
       the event — `fitt inbox` stays useful.
- [x] 6e. Tests: rate limit triggers and reports `retry_after_secs`,
       event emission on success, no-push-channel warning path.

## 7. Telegram push

- [x] 7a. `TelegramPusher` in the telegram-bot package (or
       gateway; decide where the Telegram client lives most
       cleanly).
- [x] 7b. Subscriber hook: EventLog.append fires the pusher.
- [x] 7c. Per-kind Telegram message formatting:
       - `cron_completed` → "✅ <cron name>: <body preview>".
       - `cron_failed` → "❌ <cron name>: <error>".
       - `agent_message` → "<title>: <body>".
       - `approval_requested` → "⚠ Approval needed: <tool> ..."
         with inline keyboard (reuses Phase 4 approval UI).
       - Body cap at `events.telegram_body_cap`; overflow replaced
         with "... (truncated)".
- [x] 7d. Delivery failures logged, do not block the producer.
- [x] 7e. Tests: each event kind formats correctly.

## 8. Cron CLI

- [x] 8a. `fitt cron list [--all]`.
- [x] 8b. `fitt cron add --name ... --schedule "..." --message "..."`
       with flags for `--silent` / `--auto-approve` / `--alias` /
       `--timezone`. (Accepts the same spec parser as `cron_add`:
       `every N[unit]`, `in N unit`, `at <iso|epoch>`,
       `cron <5-field>`.)
- [x] 8c. `fitt cron remove <id>`, `fitt cron pause <id>`,
       `fitt cron resume <id>`. `fitt cron run <id>` deferred —
       needs a `POST /v1/cron/<id>/run` endpoint that doesn't
       exist yet; workaround is `--schedule "in 5 seconds"`.
- [x] 8d. Tests: CLI dispatches to `CronService`
       (`tests/test_cli_cron.py`).

## 9. Inbox CLI

- [x] 9a. `fitt inbox [--since 24h|7d] [--kind <k>] [--session <s>]
       [--limit N] [--json]`.
- [x] 9b. `--since` accepts `Nh|Nd` or ISO timestamp (reuses
       `_parse_since` from the audit CLI).
- [x] 9c. Human-readable default output; `--json` for programmatic.
- [x] 9d. Tests (`tests/test_cli_inbox.py`).

## 10. Event pruning cron

- [x] 10a. `EventPruner` in `gateway/event_pruner.py` — an
       async background loop the gateway starts at boot. Not
       a user-visible entry in `cron.json` (mirrors the spec's
       "built-in cron, not user-visible"). Runs once per day
       (`prune_interval_secs = 24h`), polls every 6h. Anchor
       file at `$FITT_HOME/events.pruner.anchor` persists
       last-pruned timestamp across restarts so a gateway
       that bounces hourly still prunes on the right cadence.
- [x] 10b. The pruner emits a `system_pruned` event saying how
       many entries were removed, visible in `fitt inbox`.
       Tests: `tests/test_event_pruner.py`.

## 11. Docs

- [ ] 11a. Update gateway README with cron + event concepts.
- [ ] 11b. Quickstart: new optional step showing "create your
       first cron" via Telegram.
- [ ] 11c. Document the `silent` / `approval_mode` flags as
       first-class patterns.

## 12. Live validation

(Manual.)

- [ ] 12a. From Telegram: "every weekday at 9 AM give me a
       briefing of my open pipelines." Approve. Verify it fires
       the next weekday at 9 AM.
- [ ] 12b. From Telegram: "monitor file /tmp/test-status.json,
       tell me when it contains `done`." Agent proposes a silent
       cron with `approval_mode: auto`. Approve. Write `done` to
       the file; verify Telegram pings once.
- [ ] 12c. `fitt inbox --since 24h` shows the events from 12a-b.
- [ ] 12d. `fitt cron list` shows the active cron; `fitt cron
       remove <id>` removes it.
- [ ] 12e. Gateway restart: verify crons resume.
- [ ] 12f. Test `send_message` by asking the agent "remind me in
       30 seconds to check my email" (via an at-cron with
       silent=false).

## Definition of done

- Required tasks complete.
- `uv run pytest -q` passes.
- Live validation (12a-12f) all green.
- Author has 1 week of use without revert.
