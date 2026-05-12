# Phase 4.6 — End-to-end test harness: Requirements

> **2026-05-12 update.** U1.2 (narration lifecycle) was retired
> when the live-chat narration detector was pulled. The shape
> check now runs only in `gateway.alias_probe` and
> `gateway.alias_eval`, where the expected tool is pinned by
> the test author — a precondition live chat can't supply
> without intent-guessing. See `docs/observed-issues.md` for
> the failure mode. The U1.2 text below is preserved for
> historical context; the test file no longer exists.

## Context

Phase 4.5's debug cycle (2026-05-07) surfaced a class of bugs that
our unit tests — including the shared `_llm_stubs.py` library we
built during 4.5 — don't reliably catch. Each bug required live
testing on Telegram, log-paste diagnostics, and multi-round
debugging. Rough cost per bug: 15–30 minutes of real time and
several round trips.

The phases ahead (4.7 `project_shell`, 5 lessons, 6 spec-runner)
have the same shape — more agentic behaviour, longer cycles — and
the manual-test cost per bug grows correspondingly. A
Phase-6-style 30-minute task failing three times during debug is
an afternoon.

This phase builds a test harness that drives the full gateway
pipeline in-process (HTTP → approval → scheduler → agent loop →
events → event-pipeline-output) so regressions in any of those
layers are caught before live testing.

## User stories

### U1. The Phase 4.5 regressions stay caught

As a FITT maintainer, I want every bug we hit during Phase 4.5
live testing to have an e2e test pinning its fix, so that a
future refactor that regresses one of them fails the suite
loudly rather than re-surfacing in live use.

**Acceptance:**

- **1.1** An e2e test exists for the `cron_fired` duplicate-push
  case. Asserts that a happy-path cron firing produces exactly
  one user-visible event kind (`cron_completed`) reachable via
  `GET /v1/events`, not two.
- **1.2** An e2e test exists for the narrated-tool-call case.
  When the stubbed LLM emits a JSON-fenced tool call in
  `content` with no `tool_calls` structure, the test asserts
  both `cron_completed` and `tool_call_narrated` land in
  `/v1/events`.
- **1.3** An e2e test exists for the `fitt-smart` silent-default
  violation. Asserts that a cron whose creator didn't set
  `agent_alias` fires against whatever alias is configured as
  `fitt-default`, not against `fitt-smart` (unless `fitt-smart`
  IS `fitt-default` for that operator).
- **1.4** An e2e test exists for the detach-threshold lifecycle.
  When a chat turn's approval is slow enough to trip the detach
  threshold, the test asserts: placeholder HTTP response lands
  synchronously, `late_tool_result` event lands asynchronously,
  session memory has both halves.
- **1.5** An e2e test exists for tool-turn poisoning. When a
  session's history contains a prior failed `cron_add` turn,
  the test asserts the next request doesn't emit duplicate
  tool calls. (Fails today; pinning it documents the known
  limitation and will fail green when Phase 5 fixes memory
  persistence.)

### U2. Full lifecycle is observable

As a test author, I want to drive the full HTTP + approval +
scheduler + event-log pipeline from a single test, so that
"this bug was at the boundary between X and Y" becomes a
tractable assertion rather than a debugging session.

**Acceptance:**

- **2.1** A fixture `e2e_app` builds a gateway via
  `create_app(build_test_config(...))` with all Phase 4.5
  subsystems wired: approval middleware, cron service, cron
  scheduler, cron runner, event log.
- **2.2** A fixture `e2e_client` exposes an async HTTP client
  bound to the gateway's ASGI transport. Tests make real
  `/v1/*` requests; the gateway processes them with real
  auth, real routing, real dispatch to the stubbed LLM.
- **2.3** A fixture `e2e_approver` polls `/v1/approvals/pending`
  and POSTs `/v1/approvals/{id}/decide` according to a
  test-supplied callback. Covers the bot's role in the
  approval loop without pulling in PTB.
- **2.4** A fixture `e2e_clock` gives the test control over the
  scheduler's "now": `e2e_clock.tick(seconds=120)` advances
  the scheduler's notion of time and triggers any due crons.
  No `time.sleep` in test code.

### U3. Future phases inherit the harness

As a maintainer starting Phase 4.7 / 5 / 6, I want the harness
shape to extend to the new phase's subsystems, so that I'm
adding test files alongside existing ones rather than building
a second harness.

**Acceptance:**

- **3.1** The conftest fixtures compose cleanly: a Phase 4.7
  test can request `e2e_client` + `e2e_approver` + a new
  fixture specific to `project_shell` (e.g. a pre-registered
  project with a scripted exit code from a fake backend)
  without reshaping what exists.
- **3.2** Adding a new lifecycle test is a file copy + local
  edits. No new infrastructure per-test.

### U4. Harness stays fast and deterministic

As a developer running the test suite on every commit, I want
the e2e suite to run in seconds (not minutes), with zero
non-determinism, so that it runs on every commit and catches
regressions within one round trip.

**Acceptance:**

- **4.1** The full e2e suite (all lifecycle tests) runs in under
  10 seconds on a developer laptop.
- **4.2** Zero flakes across 100 consecutive runs.
- **4.3** No reliance on `time.sleep` or wall-clock timing in
  test bodies.
- **4.4** No network egress. No real LLM, no real Telegram API,
  no real Ollama.

### U5. Scope is honest

As a reviewer, I want the harness's non-goals enumerated in the
spec, so that scope-creep attempts (e.g. "let's also test real
PTB dispatch here") are evaluated against a documented decision
rather than re-litigated.

**Acceptance:**

- **5.1** The `design.md` includes a "non-goals" section
  naming at least: real Telegram bot API, docker-compose
  integration, real model calls, PTB internal dispatch,
  real SSH reachability.
- **5.2** Each non-goal has a one-sentence rationale pointing
  at where that concern IS covered (separate test suite,
  live validation, llm-checker, etc.) so the exclusion is
  not a blind spot.

## Definition of done

- All required tests from U1 exist, green.
- Fixtures from U2 exist and are composable per U3.
- U4 timing and determinism targets met.
- Design doc includes non-goals per U5.
- `uv run pytest -q` passes.
- Five `[x]` checkboxes in `tasks.md`: one per U1 test.
- Author has used the harness to catch one real bug during a
  subsequent Phase 4.7 / 5 commit. (If this doesn't happen
  within two weeks of merge, the phase's value assumption was
  wrong and we should revisit.)
