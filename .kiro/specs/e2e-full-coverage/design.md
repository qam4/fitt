# Design: Full e2e Coverage

**Status:** drafted 2026-08-12, not started.

## Shape

Two layers feeding one standing view.

```
                        registry (34 tools, grows)
                                  |
        +-------------------------+--------------------------+
        |                                                    |
  judged scenarios                                   tool-contract checks
  (model picks the tool)                             (harness calls the tool)
  live model + judge                                 no model, no tunnel
  slow, non-deterministic                            fast, deterministic, CI-gateable
        |                                                    |
        +-------------------------+--------------------------+
                                  |
                       fitt eval matrix / coverage
```

The split matters because the two layers answer different questions.
A judged scenario answers *"will this model reach for the right tool?"* —
model-dependent, worth measuring per model. A contract check answers
*"does this tool work and return what it claims?"* — model-independent,
so paying a live model to test it is waste, and any flake it inherits is
pure noise.

This is the "tools check" rung the capability ladder already describes in
the project overview ("are my tools well-formed / consistent... a cheap
offline check that reads whatever's registered. *Not built yet*"). This
spec builds it.

## D1. Coverage is computed from the registry

`fitt eval coverage` reads `ToolRegistry.list_names()` and subtracts what
the two layers declare. A hand-maintained list of "tools we cover" would
rot the first time someone registers a tool; deriving it means a new tool
shows up uncovered without anyone remembering to update a doc.

Declaration:
- a judged scenario declares `exercises_tools: tuple[str, ...]` (what it
  is *meant* to drive — distinct from `requires_tools`, which is what
  must exist for it to run at all);
- a contract check is keyed by tool name;
- `EXEMPT: dict[str, str]` maps tool -> reason for deliberate
  non-coverage, so 2.3 holds.

Consequence worth stating: a judged scenario's `exercises_tools` is an
*intent*, and a model that ignores the tool still leaves it unproven. So
coverage counts intent, and the matrix cell shows what actually happened.
Both numbers are honest only together.

## D2. Contract checks

One check per tool: build a `ToolContext` against a temp FITT home, call
the tool with valid args, assert `ToolResult.ok` and the side effect;
then call with invalid args and assert a *structured* error rather than an
exception. The second half is the more valuable one — a tool that raises
instead of returning `ToolResult.err` breaks the agent loop's error
handling, and no judged scenario would reveal it.

Fixtures needed: a temp project registered in the registry (for
`project_shell` / `run_tests` / git tools), a temp git repo (for
`git_status` / `git_diff` / `git_commit`), a file tree (for `read_file` /
`glob_search` / `grep_repo`), and a stub HTTP server (`http_get`).

Ordering: destructive checks (`write_file`, `cron_remove`,
`todo_remove`, `learn_remove`) run against fixtures they created
themselves, never shared state — R5.4.

## D3. `send_message` needs a sink, not a stub

Requirement 3.1 asks for an objective check on delivery. The tool sends
via the Telegram poller in production. For the harness, register a
**recording sink** on app.state that appends `(chat_id, text)`; the
scenario's `outcome_assert` reads it from the snapshot
(`snap["sent_messages"]`).

This is deliberately a *sink*, not a mock of the Telegram API: we're
verifying FITT decided to send the right thing, not that python-telegram-
bot works. Testing the wire is the telegram-bot package's job.

## D4. Cron firing: force a tick, don't sleep

Requirement 3.2. `CronRunner` polls on an interval. A scenario that
sleeps 60s to catch a fire is slow and flaky (and the steering file
forbids sleeping). Instead expose a test seam that advances/forces one
evaluation pass, then assert: the job ran, an agent session executed, and
`sent_messages` (D3) received the delivery. The setup hook (already
shipped) pre-creates the job so cancel-vs-never-created is
distinguishable (3.3).

## D5. Approval flow

Today the eval wraps approval in `_AutoApproveWrapper`, which means the
`ask` path — the thing that protects the user — is never exercised. Add
scenarios with a scripted decider: approve, reject, and timeout, then
assert the tool ran / didn't run / reported the timeout error, and that
the audit log recorded the decision either way.

## D6. Standing view gains a layer axis

`fitt eval matrix` currently keys on (scenario, dut). Contract checks
have no dut — they're deployment facts. Render them as a second table
("tool contracts: 34 checked, 0 failing"), not as extra rows with a
meaningless model column. Conflating them would imply a tool failure is
model-specific.

## Correctness properties

1. **Coverage is derived.** Register a tool, run `fitt eval coverage`,
   and it appears as uncovered without any other edit.
2. **Contract checks need no model or tunnel.** The whole layer runs in
   CI; a missing tunnel cannot make it fail.
3. **Invalid args never raise.** Every tool returns a structured error.
4. **No cross-check contamination.** Each contract check's fixtures are
   its own; running the suite twice gives identical results.
5. **Exemptions are explicit.** An uncovered tool is either reported
   uncovered or listed in `EXEMPT` with a reason. There is no silent
   third state.
6. **A judged scenario that didn't drive its declared tool leaves it
   unproven** — intent is not evidence (D1).

## Testing strategy

The contract layer is itself unit-testable: point it at a fake registry
holding one ok-tool and one raising-tool, and assert it reports the
raiser. That keeps property 3 honest without 34 live calls.
