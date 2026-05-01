# Phase 6 — Spec-Runner: Unattended Coding

## Background

Through Phase 5, FITT has tools (Phase 4), cron + event log
(Phase 4.5), and lessons + decaying history (Phase 5). Enough to
do synchronous work, schedule it, recall it.

Missing: the ability to walk away. "Work on this overnight, stop
if you hit a blocker." That requires a task runner — something
that takes a queue of work, executes it, tracks state, and
notifies when finished or stuck.

Phase 6 delivers a deliberately small version. The Kiro-style
spec-driven workflow you're already using has `tasks.md` files
with checkboxes. The runner walks those files, runs each
unchecked task in its own session, commits on success, stops on
first blocker.

That's it. Compared to industrial-strength task runners: no
planner (the `tasks.md` is the plan), no replan, no self-review,
no parallel steps, no acceptance review. Those are phase-7+
features that earn their way in once the basic thing is shipped
and lived with.

## Why now

Three prerequisites converge:

- **Phase 4's SSH-backed tools** mean the runner can execute on
  the project's host, not the gateway's host. Critical: the code
  lives on your laptop, not the NAS.
- **Phase 4.5's event log and Telegram push** mean the runner can
  notify you about progress without being in an active chat.
- **Phase 5's lessons** mean the runner inherits patterns you've
  taught (build commands, conventions, etc.) without you
  re-explaining.

With all three in place, Phase 6 is a thin wrapper around
existing primitives: spawn a session per task, execute tools,
mark the task done, emit events.

The target scenario: "work overnight on this spec, don't wake me
unless blocked." The next morning, you have a branch with
commits and a state of "3 tasks done, blocked on task 4 because
tests fail" or "12 tasks done, all green."

## Goals

1. **Walk `tasks.md`.** Parse the standard Kiro tasks file,
   identify unchecked tasks, run them in order.
2. **Worktree isolation on the execution host.** Each run
   creates a fresh git worktree on the project's `ssh_host`. All
   edits happen there; user's main checkout is untouched.
3. **Per-task session.** Each task spawns a fresh session,
   memory-injected, spec-aware. Tools: Phase 4 + 4.5 + 5.
4. **Commit per task.** After a task passes: `git add -A && git
   commit -m "<task title>"`. One commit per task on the
   worktree branch.
5. **Cycle detection.** Same error three times in a row on one
   task → stop the run.
6. **Stop on first unrecoverable failure.** No replan, no
   auto-retry beyond cycle-limit. User sees the blocker, decides
   next steps.
7. **Checkbox-based checkpointing.** `spec_mark_task` (Phase 4
   tool) updates `tasks.md`. Resume = skip already-checked tasks.
8. **Per-task timeout.** Default 30 min; configurable.
9. **Notifications per task.** Start / success / failure / run
   completion each emit an event (Phase 4.5 infrastructure).
10. **CLI + tool access.** `fitt task run <spec-dir>` from a
    terminal; `task_run` inline tool from any client.

## Non-goals (deliberate)

- **No planner.** We don't decompose free text into tasks. The
  `tasks.md` you authored is the plan.
- **No replan after failure.** Stopping is fine; the human
  decides whether to unblock.
- **No self-review via diff.** Trust tests. Trust the executor.
- **No parallel steps.** Sequential only.
- **No acceptance review at run end.** The spec's tasks.md
  sub-tasks are the acceptance.
- **No watchdog on stalled agent sessions.** Per-task timeout
  covers it.
- **No retry budgets beyond cycle detection.** A task either
  passes or fails; no five-attempts-with-backoff machinery.
- **No dashboard / web UI for runs.** Telegram + `fitt inbox`.
- **No automatic pushback or PR creation.** Just a branch on the
  host.

Each one of these is a Phase 7+ feature that earns its way in if
needed. Shipping without them is deliberate.

## User stories

### U1 — Overnight run

> As a user, at 11 PM I tell FITT on Telegram: "work on the
> phase4-tools spec overnight. Don't wake me unless blocked."
> At 7 AM I want to see the state.

Acceptance:
- Agent recognises the intent and calls `task_run(spec_dir=...,
  notify_on_progress=false)`.
- Approval prompt in Telegram ("start a task run on this spec?").
- Approve once.
- Runner creates a worktree on the project's host, walks tasks,
  commits per task.
- `notify_on_progress=false` means only start-of-run, end-of-run,
  and blocker events push to Telegram. Each task's start and
  end still logs to the event log but doesn't ping Telegram.
- Blocker events always push to Telegram.
- At 7 AM: user either has 0 Telegram messages (run still going
  or clean finish; check `fitt task status` or `fitt inbox`) or
  1-2 messages (start confirmation + blocker or completion).

### U2 — Run from a CLI

> As a user at my desk, I type `fitt task run .kiro/specs/phase4-tools`
> and watch progress on the terminal.

Acceptance:
- The CLI streams events from the event log filtered to this
  run's task_id.
- Each task: shows "starting", "running <tool call>", "passed /
  failed".
- On completion, prints a summary.

### U3 — Resume after an interruption

> As a user, my run was stopped after 3 of 10 tasks because I
> hit Ctrl-C. I want to restart it and have it skip the 3 done
> tasks.

Acceptance:
- `fitt task run <spec-dir>` the second time sees 3 tasks
  checked (because the runner called `spec_mark_task` after each
  pass) and starts from task 4.
- The same worktree is reused (if still present) or a new one
  created.
- Commits from the first run are still there.

### U4 — Stop cleanly on a blocker

> As a user, when the runner can't complete a task (tests fail
> repeatedly, a tool errors), I want a clear notification with
> the failure reason, and no further tasks attempted.

Acceptance:
- On task failure: emit a `task_failed` event with the task
  title + error. Push to Telegram.
- Subsequent tasks not attempted.
- Run ends with status `blocked`.
- `fitt task status` shows which task blocked and why.
- Worktree left intact so the user can `ssh satellite` and
  debug.

### U5 — Cycle detection

> As a user, I don't want the runner to loop on the same error
> burning time and LLM credits.

Acceptance:
- Runner tracks the last error per task. Three consecutive
  identical (or near-identical) errors on a task → mark the
  task as failed, emit `task_failed`, stop.

### U6 — Per-task timeout

> As a user, I don't want a wedged session to hold up the whole
> run.

Acceptance:
- Each task has a default timeout of
  `task_runner.per_task_timeout_secs` (default 1800 = 30 min).
- On timeout: task is marked failed with error "timeout after
  X minutes"; run stops.

### U7 — Notification shape

> As a user, I want different notifications depending on what
> I asked for.

Acceptance:
- `task_run(notify_on_progress=true)`: every task start /
  pass / fail pushes to Telegram.
- `task_run(notify_on_progress=false)`: only run-level events
  (run_started, run_completed, run_blocked) and any task
  failure push to Telegram. Task starts and successes land in
  the event log silently.
- Default is `false` (anti-spam).

## Scope boundaries

**In scope:**

- `gateway/task_runner.py` — the spec-walker + session orchestrator.
- `task_run` inline tool (Phase 4 registry).
- `fitt task run <spec-dir>`, `fitt task status`, `fitt task
  cancel <task_id>`, `fitt task list` CLI.
- Worktree management: create on project `ssh_host`, use
  throughout run, optionally keep at run end.
- Cycle detection + per-task timeout.
- Event emissions for task lifecycle.
- `tasks.md` parser (Kiro format: checkbox lines, hierarchy).
- Runs persistence: `$FITT_HOME/runs.json` with per-run status.

**Out of scope (deferred to later phases):**

- Task runner planner / free-text-to-tasks.
- Replan on failure.
- Self-review via git diff.
- Parallel task execution.
- Acceptance review at run end.
- Stall watchdog.
- Auto-PR creation.
- Worktree auto-cleanup (user decides what to do with the
  branch).
- Run-level retry / backoff.
- Distributed / multi-host runs.

## Risks and open questions

### R1 — Kiro's tasks.md format variations

**Risk:** Different projects have slightly different checkbox
formats, numbering schemes, hierarchy depths.

**Decision:** adopt a simple subset:
- `- [ ] N. Task title` = unchecked.
- `- [x] N. Task title` = checked.
- Continuation lines (indented, no bullet) = task description.
- Nested `- [ ]` lines = sub-tasks. Runner walks top-level
  checkboxes only; sub-tasks become context for the task
  session.
- Other markdown (headers, prose) treated as decoration.

Parser is lenient. Malformed lines logged and skipped. Author's
existing specs (`phase1-gateway/tasks.md`, etc.) tested as
fixtures.

### R2 — Worktree left in a bad state

**Risk:** Run crashes mid-task. Worktree has partial changes,
maybe dirty, maybe half-committed.

**Decision:**
- Each task starts with a `git status` check. If dirty (shouldn't
  happen except after a crash), the runner either cleans up
  (`git checkout .`) or stops with a "worktree dirty, manual
  cleanup needed" error — config flag `auto_clean_dirty` (default
  `false`, manual cleanup).
- Document that if a run dies, the user inspects the worktree.

### R3 — Tests fail on first task, hiding real failures later

**Risk:** Tests are broken before the run starts for unrelated
reasons. Runner fails task 1, never reaches the real work.

**Decision:** `task_run` has an optional `skip_initial_tests`
flag (default `false`). When true, the runner doesn't run tests
before the first task's changes. Documented as "use if you know
the tree is currently dirty-but-not-broken."

### R4 — Runner's session history bloats tasks.md with random content

**Risk:** Runner session prompts and replies end up in session
history, grow fast, pollute the user's `main` session.

**Decision:** runner uses a dedicated session key per run
(`task:<run_id>`), not the user's session. Isolated history
that's purgeable with the worktree.

### R5 — A single long task hogs tokens / cost

**Risk:** A poorly-scoped task keeps going, burns through the
model's output, costs $$.

**Decision:** `task_run.per_task_token_budget` config (default
unlimited; setting a number caps). Hit the cap → task fails
with "token budget exceeded."

### R6 — User not allowlisted to approve

**Risk:** `task_run` is in `ask` bucket. Approval goes to
Telegram. If the user isn't set up on Telegram, runs can't start.

**Decision:** acceptable. The runner is a power-user feature;
Telegram setup is a prereq. Documented.

### R7 — SSH session disconnects mid-task

**Risk:** A network blip drops the SSH session to the execution
host. The task fails.

**Decision:** fail the task, count toward cycle detection. If
transient, the user restarts the run. Phase 7+ could add
per-command retry with exponential backoff; not worth it now.

### R8 — Run state vs. tasks.md divergence

**Risk:** Runs.json says task 4 is in_progress; tasks.md says
task 4 is unchecked. Which wins?

**Decision:** tasks.md wins. `spec_mark_task` is the commit of
"this is done." runs.json is derived state (for listing /
reporting). On restart, rebuild runs.json from a scan of
tasks.md.

## Success criteria

Phase 6 is done when:

1. On the author's setup:
   - `fitt task run .kiro/specs/phase4-tools` walks the spec,
     commits per task, marks the checkboxes.
   - Completes a 3+ task run fully without intervention.
   - Correctly stops on a deliberate test failure introduced as
     a blocker.
2. Worktree isolation works end-to-end: a task run on a
   satellite doesn't touch the laptop's main checkout branch.
3. Event stream via `fitt inbox` shows a clean per-task story.
4. Resumption skips already-checked tasks.
5. The "overnight" scenario works: start before sleep, wake up to
   either a completion message or a clear blocker message.
6. All existing tests pass.
7. Author has used the runner on 2+ real specs.
