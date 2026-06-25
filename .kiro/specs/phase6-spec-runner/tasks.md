# Phase 6 — Spec-Runner: Tasks

**Status:** shelved

Status legend: `[x]` done, `[ ]` not yet.

## 1. `tasks.md` parser

- [ ] 1a. `gateway/task_parser.py`: `ParsedTask` dataclass +
       `parse_tasks_md(path)` function.
- [ ] 1b. Handle `- [ ]` and `- [x]` top-level bullets.
- [ ] 1c. Handle continuation lines (indented, no bullet).
- [ ] 1d. Handle nested sub-tasks (one level; deeper nesting
       flattened into the description).
- [ ] 1e. Preserve non-bullet content verbatim for round-trip
       write-back.
- [ ] 1f. `write_tasks_md(path, parsed, mark_index=N, checked=True)`
       for `spec_mark_task` integration.
- [ ] 1g. Tests: round-trip on the existing specs
       (`phase1-gateway/tasks.md`, `phase2-memory/tasks.md`,
       `phase3.5-docker-hub/tasks.md`, this file).

## 2. TaskRun data model + persistence

- [ ] 2a. `gateway/task_runner.py`: `TaskRun` and `TaskState`
       dataclasses.
- [ ] 2b. Persistence layer at `$FITT_HOME/runs.json`. Atomic
       write on every status change.
- [ ] 2c. `fitt task list` / `fitt task status <run_id>` CLI.
- [ ] 2d. Tests.

## 3. Worktree setup

- [ ] 3a. `_setup_worktree(run)` builds the right shell command
       for the project's `ssh_host`. Creates `.fitt-run/state.json`
       in the worktree.
- [ ] 3b. Error cases: worktree path already exists, branch
       already exists, git fetch fails.
- [ ] 3c. Tests with a real local git repo fixture.

## 4. Task execution: success path

- [ ] 4a. `TaskRunner.start(spec_dir, ...)` creates a run,
       parses the spec, sets up the worktree, and spawns a
       background task that drives the run.
- [ ] 4b. For each unchecked task: spawn a session, build
       the prompt, run the agent loop until it stops calling
       tools, then call `_run_tests` on the worktree.
- [ ] 4c. On pass: commit via `_commit_task`, call
       `spec_mark_task` (Phase 4) to tick the checkbox, emit
       `task_passed`.
- [ ] 4d. Advance to next task.
- [ ] 4e. Tests: mock the agent loop + SSH backend; exercise a
       3-task run through to completion.

## 5. Failure handling + stopping

- [ ] 5a. On agent exception: mark task failed, emit
       `task_failed`, stop the run.
- [ ] 5b. On test failure: same.
- [ ] 5c. On timeout (asyncio.wait_for): same.
- [ ] 5d. On cancel: set status `cancelled`, emit `run_cancelled`.
- [ ] 5e. Run's final state: `completed` (all tasks passed),
       `blocked` (a task failed), or `cancelled` (user stopped).
- [ ] 5f. Tests.

## 6. Cycle detection

- [ ] 6a. `_detect_cycle(task, new_error)`: normalise errors,
       track last 3, fire if all three are identical after
       normalisation.
- [ ] 6b. Integration: a task that the agent retries three
       times with the same error gets stopped with
       `cycle_detected` in the error text.
- [ ] 6c. Tests.

## 7. Per-task timeout + token budget

- [ ] 7a. `asyncio.wait_for` around the per-task agent loop.
- [ ] 7b. Token budget: track prompt + completion tokens across
       the task's tool loop; abort when over budget with a
       clear error.
- [ ] 7c. Tests.

## 8. Events + Telegram push filter

- [ ] 8a. Emit `run_started`, `run_completed`, `run_blocked`,
       `run_cancelled`, `task_started`, `task_passed`,
       `task_failed`.
- [ ] 8b. Telegram push respects `notify_on_progress`:
       progress events (task_started/passed) suppressed if
       `notify_on_progress=false`; failures always pushed.
- [ ] 8c. Tests with the mock Telegram pusher.

## 9. `task_run` tool

- [ ] 9a. Register in Phase 4's tool registry.
- [ ] 9b. Bucket: `ask` (starting a run is a commitment).
- [ ] 9c. Tests.

## 10. CLI

- [ ] 10a. `fitt task run <spec-dir> [--project <name>]
       [--progress] [--timeout <min>] [--follow]`.
- [ ] 10b. `fitt task list`.
- [ ] 10c. `fitt task status <run_id>`.
- [ ] 10d. `fitt task cancel <run_id>`.
- [ ] 10e. `fitt task tail <run_id>` (wraps `fitt inbox`).
- [ ] 10f. Tests.

## 11. System prompt additions

- [ ] 11a. Task session's system prompt includes a hint:
       "You are executing a single task in a multi-task spec.
       Do not run tests; the runner will. Stay focused on THIS
       task."
- [ ] 11b. Carries identity + lessons + (session-less) history
       per standard context builder.

## 12. Docs

- [ ] 12a. Gateway README new section: "Spec Runner" with
       usage examples.
- [ ] 12b. Document the expected `tasks.md` shape and how the
       runner reads/writes it.
- [ ] 12c. Document how worktrees clean up (they don't; manual).

## 13. Live validation

(Manual.)

- [ ] 13a. Write a toy spec (3-task `tasks.md`) in a test
       project. Run `fitt task run` on it. Verify all three
       tasks pass, worktree has 3 commits, original branch
       untouched.
- [ ] 13b. Inject a deliberately failing test in task 2. Verify
       runner stops cleanly at task 2; task 3 is not started.
       Verify blocker event pushed to Telegram.
- [ ] 13c. Re-run the same spec after fixing the blocker.
       Verify tasks 1-2 are skipped (already checked), task 3
       executes.
- [ ] 13d. Start a run from Telegram: `run phase4-tools.` Approve.
       Verify it starts and the right events flow.
- [ ] 13e. Overnight run against a real spec (the Phase 4 spec
       itself, with tasks de-facto "do nothing" to test the
       machinery). Wake up, verify state.
- [ ] 13f. Cycle detection: arrange for the same error 3 times;
       verify the task fails out with `cycle_detected`.
- [ ] 13g. Timeout: set `per_task_timeout_secs=10` and a task
       that sleeps 30s; verify the task fails with timeout.

## Definition of done

- All required tasks complete.
- `uv run pytest -q` passes.
- Live validation (13a-13g) all green.
- Author has used the runner on at least 2 real specs.
- No regressions in Phase 1-5 behavior.
