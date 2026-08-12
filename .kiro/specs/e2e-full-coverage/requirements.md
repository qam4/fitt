# Requirements: Full e2e Coverage

**Status:** drafted 2026-08-12, not started.

## Why

`fitt eval matrix` now answers "which models can drive which features" —
but only for the 7 seed scenarios, which exercise **7 of 34 registered
tools**. The standing matrix therefore says *"the daily-use core works on
gemma4"*, not *"FITT works"*. Whole surfaces have never been exercised:
proactive `send_message`, a cron actually firing, repo reads, the
approval flow, Telegram, the skills loader, planned-mode orchestration,
prefetch.

The goal is a standing view that covers everything FITT ships, so a
regression anywhere shows up as a red cell rather than as a surprise in
daily use.

## Scope note: covering a tool is not investing in it

The scope doc says FITT is not a coding agent, and that write-side code
tools are not on the critical path. That governs *where we invest*, not
*what we verify*: `write_file` and `git_status` are registered and
reachable today, so if they're broken FITT is broken. They get coverage
that proves they work, and no further attention.

## User stories

### 1. Two layers, matched to what each can actually prove

**1.1** As an operator I want *judged scenarios* for capabilities a user
asks for in words ("remind me", "what's my locker number"), because
those test tool **selection** — the model choosing correctly among 34
options — which is the part that varies by model.

**1.2** As an operator I want a deterministic *tool-contract layer* for
the whole registry, because 34 judged scenarios would be slow, flaky and
expensive, and most tools need no model to verify: call the tool, assert
its declared result shape and its side effect.

**1.3** As an operator I want both layers in one standing view, marked by
layer, so I can tell "the model won't reach for this" apart from "this
tool is broken".

**1.4** The tool-contract layer MUST run without a live model or a
tunnel, so it can gate CI while judged scenarios stay a dev/debug driver.

### 2. Every registered tool is accounted for

**2.1** A tool in the registry that has neither a judged scenario nor a
contract check MUST be reported as uncovered — coverage is computed from
the registry, not from a hand-maintained list, so a newly registered
tool starts life visibly uncovered.

**2.2** `fitt eval coverage` reports covered / uncovered counts and names.

**2.3** Deliberately-uncovered tools MUST be declared with a reason, so
the difference between "we chose not to" and "we forgot" is explicit.

### 3. Proactive behaviour is verified end to end

**3.1** `send_message` delivery MUST be verifiable by an objective check:
a test sink records outbound messages and the assertion reads it.

**3.2** A cron job MUST be shown to *fire*, run its agent session, and
deliver — not merely to have been created. Prefer a forced tick over
sleeping.

**3.3** Cancel/pause MUST be distinguishable from never-created, via the
scenario setup hook.

### 4. Untested surfaces get coverage or an explicit exemption

Approval flow (ask -> approve/reject/timeout), Telegram command handling,
skills loading, planned-mode orchestration, prefetch injection. Each
either gets a check or a declared exemption with a reason (2.3).

### 5. Honesty properties carried over from the seed set

**5.1** A missing prerequisite reports unsupported, never a model
failure (`requires_tools`).

**5.2** A run that didn't exercise its target reports inconclusive.

**5.3** A tool never measured for a model shows `-`, not `FAIL`.

**5.4** No scenario may depend on another's side effects; shared global
state (lessons, todos, crons, index) has already produced one
cross-scenario false verdict.

## Non-goals

- Making FITT better at code editing (scope doc).
- Judged scenarios for every tool — that's what the contract layer is
  for.
- Replacing the existing unit/integration suites. This measures the
  live-ish surface, not internals.
