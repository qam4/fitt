# Phase 6 — Spec-Runner: Design

## Overview

A task runner that walks a Kiro-style `tasks.md`, executes each
unchecked task in an isolated session, commits on success, stops
on first blocker. Worktree-isolated on the project's execution
host so the user's main checkout stays untouched.

## Architecture

```
            User (Telegram or CLI)
                  |
                  v
        +--- Gateway (hub) ------------+
        |                              |
        |   task_run inline tool       |
        |     | check approval         |
        |     v                        |
        |   TaskRunner.run(spec_dir)   |
        |     |                        |
        |     v                        |
        |   parse tasks.md             |
        |   create worktree on host    |   ssh satellite.tailnet 'git worktree add ...'
        |   for each unchecked task:   |   ------------------------------------------>
        |     spawn fresh session      |
        |     run task loop            |
        |       +- tools via ssh ----- |   ssh satellite 'cat/edit/test'
        |       | (all Phase 4 tools   |   ----------------------------------------->
        |       |  with ssh backend)   |
        |     +- on pass: commit       |   ssh satellite 'git commit ...'
        |     +- on fail: stop         |
        |     +- emit events           |
        |                              |
        +------------------------------+
                  |
                  v
            $FITT_HOME/events.jsonl  ---> Telegram push (Phase 4.5)
            $FITT_HOME/runs.json     (status for fitt task status)
```

Three principles driving design.

1. **Worktree isolation is the safety boundary.** The runner
   can do anything it wants inside the worktree branch. Outside,
   no access — file tools are scoped to the worktree path.
   Destructive mistakes confined to a throwaway branch.
2. **The spec is the plan.** We don't derive tasks from anywhere
   except the human-authored `tasks.md`. Runner is a walker, not
   a planner.
3. **Fail cleanly, stop.** No replan, no auto-fix, no guesswork.
   If a task fails, the human decides. This keeps the runner's
   behavior predictable and debuggable.

## Data model

### TaskRun

```python
@dataclass
class TaskRun:
    id: str                           # e.g. "phase4-tools_1771822344"
    spec_dir: str                     # absolute path (on gateway's view)
    project_name: str                 # resolved from spec_dir
    ssh_host: str                     # empty = hub-local
    worktree_path: str                # on the execution host
    branch_name: str                  # e.g. "fitt/phase4-tools-1771822344"
    base_branch: str                  # branch the worktree forked from
    status: Literal["pending", "running", "completed", "blocked", "cancelled"]
    current_task_idx: int             # 0-based into the task list
    tasks: list[TaskState]            # snapshot at run start
    started_at: float
    finished_at: float                # 0 while running
    commit_hashes: list[str]          # one per passed task
    notify_on_progress: bool
    per_task_timeout_secs: int
    per_task_token_budget: int         # 0 = unlimited
    error: str                        # for `blocked` / `cancelled`


@dataclass
class TaskState:
    index: int                        # position in tasks.md
    title: str
    description: str                  # full continuation text
    status: Literal["pending", "in_progress", "passed", "failed"]
    attempts: int                     # for cycle detection
    last_error: str
```

Persisted to `$FITT_HOME/runs.json` as a JSON array. Runs older
than `runs.max_age_days` (default 30) pruned by the same nightly
cron that prunes history.

### Parsed tasks.md

```python
@dataclass
class ParsedTask:
    index: int                        # sequence in the file
    checked: bool
    title: str                        # the bullet's main line
    raw_label: str                    # e.g. "1.", "1a.", "-"
    description_lines: list[str]      # continuation lines (indented)
    subtasks: list[ParsedTask]        # recursive
    line_number: int                  # for spec_mark_task round-trip
```

The parser is lenient:
- Matches `- [ ]` and `- [x]` (case-insensitive `x`).
- Continuation lines: any indented non-bullet line after a
  bullet, up to the next bullet at the same or lower indent.
- Sub-tasks: nested `- [ ]` lines.
- Non-matching lines: preserved verbatim on write-back (for
  headers, prose, etc.).

### Worktree metadata

Stored under the worktree root as `.fitt-run/state.json`. Contains
the run ID so you can `cd` to any worktree and know which run
owned it.

## Module design

### `gateway/task_runner.py`

```python
class TaskRunner:
    def __init__(
        self,
        projects: ProjectRegistry,
        tool_registry: ToolRegistry,
        event_log: EventLog,
        sessions: SessionRegistry,
        gateway: Gateway,
    ) -> None: ...

    # Public API

    async def start(
        self, spec_dir: Path,
        notify_on_progress: bool = False,
        per_task_timeout_secs: int | None = None,
        skip_initial_tests: bool = False,
    ) -> TaskRun: ...

    def cancel(self, run_id: str) -> bool: ...

    def list(self) -> list[TaskRun]: ...
    def get(self, run_id: str) -> TaskRun | None: ...

    # Internal

    async def _run_loop(self, run: TaskRun) -> None: ...
    async def _execute_task(self, run: TaskRun, task: TaskState) -> bool: ...
    def _parse_tasks_md(self, path: Path) -> list[ParsedTask]: ...
    async def _setup_worktree(self, run: TaskRun) -> None: ...
    async def _commit_task(self, run: TaskRun, task: TaskState) -> str: ...
    def _detect_cycle(self, task: TaskState, new_error: str) -> bool: ...
```

### Task execution per-task

```python
async def _execute_task(self, run, task):
    # Fresh session scoped to this run
    session_key = f"task:{run.id}:{task.index}"

    # Build the prompt
    prompt = build_task_prompt(
        run=run,
        task=task,
        tasks_md_context=self._passed_tasks(run),
    )

    # Loop: model reasons + calls tools; runner enforces timeout.
    start_ts = time.time()
    try:
        await asyncio.wait_for(
            self._agent_loop(session_key, prompt, run),
            timeout=run.per_task_timeout_secs,
        )
    except asyncio.TimeoutError:
        task.last_error = f"timeout after {run.per_task_timeout_secs}s"
        task.status = "failed"
        return False
    except Exception as e:
        task.attempts += 1
        task.last_error = str(e)
        if self._detect_cycle(task, str(e)):
            task.status = "failed"
            return False
        # retry once if not a cycle -> next iteration
        # actually: no retry. Treat any exception as failure.
        task.status = "failed"
        return False

    # Test the result
    if not await self._run_tests(run):
        task.last_error = "tests failed"
        task.status = "failed"
        return False

    # Commit
    commit = await self._commit_task(run, task)
    run.commit_hashes.append(commit)
    task.status = "passed"
    return True
```

The `_agent_loop` runs the same chat-handler loop that
user-initiated sessions use: model gets the prompt, calls tools,
gets results, eventually produces a final reply. The runner
considers the task "complete" when the model stops calling tools
and produces a final message. Then tests run as a separate step.

### Worktree setup

```python
async def _setup_worktree(self, run):
    project = self._projects.get(run.project_name)
    ts = int(run.started_at)
    run.branch_name = f"fitt/{spec_slug}-{ts}"

    # On the execution host:
    #   1. git fetch (optional; makes sure we branch from latest)
    #   2. git worktree add <worktree_path> -b <branch>
    #   3. mkdir .fitt-run; write state.json
    cmd = (
        f"cd {shlex.quote(project.path)} && "
        f"git fetch && "
        f"git worktree add {shlex.quote(run.worktree_path)} "
        f"-b {shlex.quote(run.branch_name)} && "
        f"mkdir -p {shlex.quote(run.worktree_path)}/.fitt-run && "
        f"echo '{{\"run_id\": \"{run.id}\"}}' > "
        f"{shlex.quote(run.worktree_path)}/.fitt-run/state.json"
    )
    result = await self._backend.run_shell_raw(
        host=project.ssh_host, cmd=cmd, timeout_secs=60,
    )
    if result.exit != 0:
        raise WorktreeSetupFailed(result.stderr)
```

Worktree path: configurable, defaults to
`{project.path}/../worktrees/{run.id}/`. Lives on the execution
host, outside the project's main checkout.

All tool calls during the run point at the worktree: the runner
overrides `project.path` to the worktree for this session.
Achieved by passing a `project_override` into the ToolContext
that the SSH backend uses for `cd` before each command.

### Prompt for a task

```python
def build_task_prompt(run, task, passed_tasks):
    return f"""\
You are executing task {task.index + 1} of a multi-task spec run.

Project: {run.project_name}
Worktree: {run.worktree_path}
Branch: {run.branch_name}

# Spec context
{read_text(run.spec_dir / 'requirements.md')[:4000]}

# Design context (excerpt)
{read_text(run.spec_dir / 'design.md')[:4000]}

# Passed tasks so far
{format_passed(passed_tasks)}

# Current task
{task.title}

{task.description}

# Instructions
- Make the changes this task requires.
- Use the available tools (read_file, edit_file, etc.).
- Do not run tests; the runner will run them after you finish.
- When you believe the task is complete, reply with a brief
  summary of what you changed. Do NOT call further tools at
  that point.
- On an unrecoverable problem, say so clearly; do NOT try
  workarounds that skip the task.
"""
```

Short and constrained. The task's own `description_lines` from
tasks.md carry the detail.

### Commit per task

```python
async def _commit_task(self, run, task):
    project = self._projects.get(run.project_name)
    msg = f"Phase 6: {task.title}\n\n(automated by fitt task run {run.id})"
    cmd = (
        f"cd {shlex.quote(run.worktree_path)} && "
        f"git add -A && "
        f"git commit -m {shlex.quote(msg)}"
    )
    result = await self._backend.run_shell_raw(
        host=project.ssh_host, cmd=cmd, timeout_secs=60,
    )
    if result.exit != 0:
        # Dirty tree check is separate; here "nothing to commit"
        # is fine too
        if "nothing to commit" in result.stdout:
            return ""    # no-op task; no commit
        raise CommitFailed(result.stderr)
    # Capture the SHA
    sha_result = await self._backend.run_shell_raw(
        host=project.ssh_host,
        cmd=f"cd {shlex.quote(run.worktree_path)} && git rev-parse HEAD",
        timeout_secs=10,
    )
    return sha_result.stdout.strip()
```

### Cycle detection

```python
def _detect_cycle(self, task, new_error):
    # Normalise the error (strip line/column numbers, timestamps)
    normalised = _normalise_error(new_error)
    if not hasattr(task, "_recent_errors"):
        task._recent_errors = []
    task._recent_errors.append(normalised)
    task._recent_errors = task._recent_errors[-3:]
    return len(task._recent_errors) >= 3 and len(set(task._recent_errors)) == 1
```

Cycle is detected after a task sees the same error 3 times
consecutively. In the current design, the runner doesn't auto-
retry on exceptions (each exception → task failure), so in
practice cycle detection fires only if the runner is modified to
retry. For Phase 6 v0, this is mostly a placeholder; a future
phase with retry-on-transient-failure would use it.

### Events emitted

- `run_started` — title includes spec name, total tasks.
- `task_started` — title is task name. Only pushed to Telegram
  if `notify_on_progress=true`.
- `task_passed` — title + commit SHA. Same filter.
- `task_failed` — title + error. Always pushed to Telegram
  (blockers matter).
- `run_completed` — final summary (N/M tasks passed, duration,
  branch name, run ID). Always pushed.
- `run_blocked` — failed on task N. Always pushed.
- `run_cancelled` — user cancelled. Always pushed.

### The `task_run` tool

```python
@tool(
    name="task_run",
    description="Start an autonomous run against a spec's tasks.md",
    schema={
        "type": "object",
        "properties": {
            "spec_dir": {"type": "string",
                         "description": "path relative to the project root"},
            "project": {"type": "string"},
            "notify_on_progress": {"type": "boolean", "default": False},
            "per_task_timeout_minutes": {"type": "integer", "default": 30},
            "skip_initial_tests": {"type": "boolean", "default": False},
        },
        "required": ["project", "spec_dir"],
    },
    bucket=ApprovalBucket.ASK,
    requires_project=True,
)
async def task_run(args, context) -> ToolResult:
    run = await context.task_runner.start(...)
    return ToolResult.ok(f"Started run {run.id}. Notifications: "
                         f"{'progress' if run.notify_on_progress else 'blockers only'}.")
```

### CLI

- `fitt task run <spec-dir> [--project <name>] [--progress] [--timeout M]`
  — starts a run, prints the run ID. Optionally tails the event
  log until the run finishes (default: detached).
- `fitt task list` — list runs (running + recent).
- `fitt task status <run_id>` — full status including each task.
- `fitt task cancel <run_id>` — request cancellation.
- `fitt task tail <run_id>` — tail events filtered to this run.

## Configuration additions

### `config.yaml`

```yaml
task_runner:
  per_task_timeout_secs: 1800         # 30 min
  per_task_token_budget: 0            # 0 = unlimited
  worktree_base: ""                   # empty = sibling of project path
  runs_max_age_days: 30               # for the runs.json pruner
  auto_clean_dirty_worktree: false
```

## Tests

### Unit

- `test_tasks_md_parser.py`: real fixtures from existing specs
  parse correctly. Malformed lines don't crash.
- `test_task_runner.py`: mock `_agent_loop`, mock SSH backend.
  Exercise run lifecycle, task commit per pass, cycle detection,
  timeout, cancellation.
- `test_task_run_tool.py`: tool dispatches to TaskRunner.start
  correctly; returns a useful response.
- `test_worktree_setup.py`: correct shell command built for both
  hub-local and ssh_host projects.

### Property-based

- **tasks.md round-trip**: parse → serialise → parse produces the
  same state. Checkbox toggles are lossless.

### Integration

- End-to-end: a toy project with a 3-task tasks.md and a test
  command. Runner walks it, all three pass, 3 commits on a new
  branch, run_completed event fires. Worktree inspectable.
- Deliberate blocker: one task in the middle fails tests.
  Runner stops at that task; later tasks untouched;
  run_blocked event fires.
- Resume: after a blocker, user "fixes" (re-marks the task as
  passed) and re-runs. Later tasks now execute.

## Rollout

1. `tasks.md` parser + round-trip tests.
2. TaskRun / TaskState data model + persistence.
3. Worktree setup shell invocations, isolated from the rest.
   Tested with a real git repo fixture.
4. Minimal `_execute_task` end-to-end without cycle detection or
   timeout: single-pass success path.
5. Commit-per-task logic.
6. Failure + stop-run logic.
7. Cycle detection + timeout.
8. Event emissions wired to Phase 4.5 event log.
9. `task_run` inline tool.
10. CLI.
11. Integration tests.
12. Live validation.

## Interactions

- **Phase 4 tools**: every tool call inside a task session uses
  the standard approval pipeline, but the task's `approval_mode`
  (set by `task_run` caller, default = auto) overrides per-tool
  `ask` buckets. Same mechanism as cron's `approval_mode`.
- **Phase 4.5 events**: all run/task events flow through the
  event log and thus Telegram push.
- **Phase 5 lessons**: each task session inherits lessons via the
  system prompt. "Always use rg" is seen; "monitor training"
  patterns come along.
- **Sessions (Phase 2.5)**: each task is its own session
  (`task:<run_id>:<task_idx>`). Isolated from the user's `main`
  session. Shared identity + lessons.

## Open design decisions

1. **Worktree cleanup policy.** Left alone by default. Phase 6
   doesn't auto-delete or auto-merge. User decides: review on
   the satellite, maybe `git push`, maybe merge to main, maybe
   delete the branch. Future phase could add a
   `fitt task archive <run_id>` to delete the worktree + branch.

2. **What if a task says "add a test"?** Agent may add a failing
   test first, then code, then pass. The "tests pass after task"
   check would fail on the test-only commit. Phase 6 doesn't
   split test / implement cleanly. Either:
   - The tasks.md author pairs test + implementation in one task
     (cleanest).
   - Or we add a `tests_after_n_tasks` hint later.
   Decision: take option A for v0. The author structures
   tasks.md so each one passes tests on its own.

3. **Running tests as a separate step.** Current design: runner
   does `_run_tests` after the agent's loop. Alternative: let
   the agent call `run_tests` itself within the loop. Simpler
   to reason about when it's the runner's separate step, but
   less flexible (the agent can't iterate on test output without
   a retry). v0 picks the simple "runner runs tests" version;
   the agent is told "do not run tests; the runner will."

4. **Detached vs attached CLI.** `fitt task run` default behaviour:
   start the run and return immediately (detached). `--follow`
   tails the event log. Alternative: default attach, `--detach`
   for background. Pick detached because that's how Unix-y users
   expect long-running jobs to behave (cf. `make` vs `nohup
   make`).

5. **Run ID format.** `{spec_slug}_{unix_ts}` is friendly but
   collision-prone if two runs start in the same second. Accept
   for v0; add `{spec_slug}_{unix_ts}_{4-hex}` if needed.
