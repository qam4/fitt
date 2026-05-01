# Phase 4.5 — Cron + Proactive Notifications: Tasks

Status legend: `[x]` done, `[ ]` not yet.

## 1. Event log primitive

- [ ] 1a. `gateway/events.py`: `EventEntry` dataclass, `EventLog`
       with `append`, `read` (with `since` / `kind` /
       `session` / `limit` filters), `prune` (delete old entries).
- [ ] 1b. Persistence at `$FITT_HOME/events.jsonl`. One JSON
       object per line. Append uses `open(..., 'a')`, flush after
       each write.
- [ ] 1c. Tests: round-trip, filters, pruning (streamed rewrite).

## 2. Cron data model + persistence

- [ ] 2a. `gateway/cron.py`: `CronSchedule`, `CronJob` dataclasses.
- [ ] 2b. `CronService` CRUD: `add`, `update`, `remove`, `enable`,
       `get`, `list`. Persistence to `$FITT_HOME/cron.json` with
       atomic writes + fcntl lock.
- [ ] 2c. Mtime-based sync: reload on external file change.
- [ ] 2d. Schedule-string parser: `"every 60s"`, `"every 5m"`,
       `"at 2026-05-01T09:00"`, `"in 30 minutes"`, `"cron 0 9 * * *"`.
       Returns a `CronSchedule`.
- [ ] 2e. `next_run_ts(job, now)` computation for all three
       schedule kinds.
- [ ] 2f. Tests: CRUD, persistence, schedule parsing, next-run
       logic.

## 3. Cron inline tools

- [ ] 3a. `cron_add`, `cron_list`, `cron_update`, `cron_remove`,
       `cron_pause`, `cron_resume`. Each calls the corresponding
       `CronService` method.
- [ ] 3b. Register with the Phase 4 tool registry. Default buckets:
       `cron_list` / `cron_pause` / `cron_resume` = `auto`;
       `cron_add` / `cron_update` / `cron_remove` = `ask`.
- [ ] 3c. Tests.

## 4. Scheduler loop

- [ ] 4a. `CronService.start()` starts an asyncio timer loop.
       Every 30s (or sooner if a job is due): scan `list()`, fire
       due jobs as independent asyncio tasks.
- [ ] 4b. `_is_due(job, now)` for each schedule kind.
- [ ] 4c. Fired jobs run under `asyncio.wait_for(..., timeout_secs)`.
       Timeout → `last_status="error"`, `last_error="timeout"`.
- [ ] 4d. `_fire(job)` calls the registered `on_fire(job)` callback.
- [ ] 4e. Tests with a 1-second interval and a no-op callback.

## 5. Cron firing → agent session integration

- [ ] 5a. Register `on_fire` callback in the gateway startup that
       spawns a fresh session (`session_key = "cron:{id}:{ts}"`)
       and runs `job.message` through the agent loop.
- [ ] 5b. Apply `job.approval_mode` as an override on the approval
       middleware for this session.
- [ ] 5c. If `silent == False`, emit `cron_completed` event with
       the agent's final reply as body.
- [ ] 5d. If `silent == True`, emit `cron_completed` with an
       empty body.
- [ ] 5e. Always emit `cron_fired` at the start.
- [ ] 5f. On error: emit `cron_failed` with the traceback
       summary.
- [ ] 5g. Integration test: create a cron, wait a firing,
       verify session spawned + events emitted.

## 6. `send_message` tool

- [ ] 6a. Inline tool with bucket `auto`.
- [ ] 6b. Per-session rate limiter (configurable).
- [ ] 6c. Emits `agent_message` event on success.
- [ ] 6d. Tests: rate limit, event emission.

## 7. Telegram push

- [ ] 7a. `TelegramPusher` in the telegram-bot package (or
       gateway; decide where the Telegram client lives most
       cleanly).
- [ ] 7b. Subscriber hook: EventLog.append fires the pusher.
- [ ] 7c. Per-kind Telegram message formatting:
       - `cron_completed` → "✅ <cron name>: <body preview>".
       - `cron_failed` → "❌ <cron name>: <error>".
       - `agent_message` → "<title>: <body>".
       - `approval_requested` → "⚠ Approval needed: <tool> ..."
         with inline keyboard (reuses Phase 4 approval UI).
       - Body cap at `events.telegram_body_cap`; overflow replaced
         with "... (truncated)".
- [ ] 7d. Delivery failures logged, do not block the producer.
- [ ] 7e. Tests: each event kind formats correctly.

## 8. Cron CLI

- [ ] 8a. `fitt cron list [--all]`.
- [ ] 8b. `fitt cron add --name ... --every N --message "..."`,
       variants for `--cron` and `--at`.
- [ ] 8c. `fitt cron remove <id>`, `fitt cron pause <id>`,
       `fitt cron resume <id>`, `fitt cron run <id>` (fire once
       now).
- [ ] 8d. Tests: CLI dispatches to `CronService`.

## 9. Inbox CLI

- [ ] 9a. `fitt inbox [--since 24h|7d] [--kind <k>] [--session <s>]
       [--limit N] [--json]`.
- [ ] 9b. `--since` accepts `Nh|Nd` or ISO timestamp.
- [ ] 9c. Human-readable default output; `--json` for programmatic.
- [ ] 9d. Tests.

## 10. Event pruning cron

- [ ] 10a. Add a built-in cron (not user-visible in
       `cron.json`) that fires daily at 04:00 local and calls
       `EventLog.prune(max_age_days)`.
- [ ] 10b. The pruner emits a `system_pruned` event saying how
       many entries were removed.

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
