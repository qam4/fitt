# Implementation Plan: Judged End-to-End Harness

**Status:** not started

## Overview

Build the pure, testable core first (scenario → run → objective outcome
check → optional judge → report, over injected fakes), then the judge
provider, then the driver wiring the real pipeline + side-effect
snapshot, then the seed scenarios, then use it to drive the todo
feature test-first. Mirrors chess-coach `game-coaching-eval` and
extends `scenario_eval`: the deterministic core is proven before any
live model / kiro-cli dependency. Objective outcome assertions are the
primary layer; the frontier judge is off by default.

Status legend: `[x]` done, `[ ]` not yet.

## Phase A — Pure core (no live deps)

- [x] 1. `gateway/src/gateway/e2e_eval.py`: value types (`RunResult`,
  `E2ETrajectory`, `OutcomeResult`, `JudgeVerdict`, `E2EResult`,
  `E2EReport`, `TaskScenario`) with `to_dict`/`from_dict`; the injected
  port types (`DispatchFn`, `SnapshotFn`, `OutcomeAssert`, `JudgeFn`).
  (Design; R2.1, R6.1) DONE 2026-07-02.
- [x] 2. `run_scenario(scenario, *, dispatch, snapshot, judge=None)`:
  send turns via `dispatch`, capture `snapshot`, run
  `scenario.outcome_assert`, optionally judge; judge-optional +
  failure-isolated (a judge error → un-judged verdict). `aggregate`
  computes objective and judge pass-rates *separately*. Plus
  `ensure_distinct_judge` (P6 guard). (R3, R4.3, R7; Properties 1, 3, 5)
  DONE 2026-07-02.
- [x] 3. Tests (fakes only): P1 run faithfulness (single + multi-turn,
  turns in order), P2 trajectory round-trip, P3 judge-off and
  judge-raises → un-judged not aborted, P4 objective assertion runs
  with no LLM (+ exception → fail), P5 aggregate separates objective vs
  judge pass-rate, P6 self-judge guard. DONE 2026-07-02: 11 tests,
  ruff/mypy/pytest green (gateway 1783).

## Phase B — Judge provider

- [ ] 4. `gateway/src/gateway/e2e_judge.py`: `CliJudge` (headless
  kiro-cli, stdin prompt → stdout verdict, temp 0), a `JudgeInput`
  builder (intent + rubric + reply + tool_sequence + objective
  outcome), and a lenient verdict parser (JSON `{passed, score,
  reasoning}` + fallback). Provider/parse error → un-judged verdict.
  (R4.1, R4.2, R4.3; Design D3)
- [ ] 5. Tests: fake Cli returns canned JSON → parsed verdict; raising
  / garbage-output Cli → un-judged. No real kiro-cli in the test.

## Phase C — Driver

- [ ] 6. `fitt eval e2e` (extend the `eval` CLI group): `dispatch`
  sends scenario turns through the real pipeline against the DUT alias
  reusing `scenario_eval`'s driver (+ transient handling + tunnel-down
  skip); optional `RecordingRouter` cassette. (R1.1, R2.2, R6.2, R6.3)
- [ ] 7. `snapshot`: read cron jobs (`app.state.cron`), `todos.md`,
  memory/history, and the event log into a plain dict at run end.
  (R3.1)
- [ ] 8. Guards + mechanics: refuse DUT==judge (P6); `--judge` off by
  default; `--samples`, `--out`, `--judge-command` configurable; warn
  cleanly when the tunnel is down. (R4.2, R4.3, R5, R6)

## Phase D — Seed scenarios

- [ ] 9. `e2e_scenarios.py` — **reminder**: turn "remind me … tomorrow
  at 9am"; `outcome_assert` = a one-shot cron with `at_ts` within
  tolerance of tomorrow 09:00 in the tz. (R7.1, R3.2)
- [ ] 10. **news_summary**: existing news scenario; light objective
  layer (substantive reply + web_search in tool_sequence) + a **rubric
  that judges summary quality** (grounded, on-topic, substantive).
  (R7.1, R4.4)
- [ ] 11. **memory_recall** (Phase 9): multi-turn scenario (state a
  fact, drain the indexer, ask about it); `outcome_assert` =
  `memory_search` in the tool_sequence AND its excerpt matches turn 1;
  rubric judges groundedness. Closes Phase 9's one unproven link (does
  the real model decide to call `memory_search`). (R7.1)
- [ ] 12. Tests: each scenario's `outcome_assert` passes on a crafted
  good snapshot and fails on a bad one (fake snapshots — no live run).

## Phase E — Todo feature, test-first

- [ ] 13. **todo** scenario + `outcome_assert` = `todos.md` gained the
  item; write it FIRST (fails — no feature yet). (R7.1)
- [ ] 14. Build the `todo_*` tool group + markdown store
  (`$FITT_HOME/todos.md`), modeled on Phase 5 `learn_*`
  (`todo_add`/`todo_list`/`todo_done`/`todo_remove`); register in the
  core registry; wire onto `ToolContext`. Iterate until the objective
  assertion passes.
- [ ] 15. Unit tests for the todo store + tools (mirror the lessons
  tests).

## Phase F — Smoke, docs, first live run

- [ ] 16. CI-safe smoke: one scenario through the Phase 4.6 e2e app
  (stubbed LLM) + a fake judge — proves driver wiring without a live
  model. (R8.3)
- [ ] 17. Full `uv run pytest` / `mypy src` / `ruff check` / `ruff
  format --check` green in both packages. (R8.1)
- [ ] 18. Docs note + kiro-monitor launch example; first live run
  (reminder + news + todo + memory_recall vs `fitt-ec2-qwen3`, judge
  on); record findings in BACKLOG/observed-issues. Roadmap/BACKLOG
  pointer.

## Verification (manual, needs the tunnel)

- [ ] V1. Reminder scenario live: FITT sets a one-shot cron for ~9am
  tomorrow; the objective assertion passes; `fitt cron list` confirms.
- [ ] V2. News scenario live with `--judge`: the judge scores summary
  quality (not just "did it fetch"); a deliberately-bad summary is
  caught.
- [ ] V3. Todo scenario live: FITT adds the item to `todos.md`; the
  objective assertion passes.
- [ ] V4. Memory-recall scenario live (Phase 9): after stating a fact
  and asking about it later, the real DUT calls `memory_search` and the
  reply is grounded in the recalled fact — closing the one Phase 9 link
  the provider/indexer tests can't (the model's decision to retrieve).
- [ ] V5. A judge/tunnel failure yields an un-judged / transient
  result, not a crashed batch.

## Definition of done

- Pure core (Phase A) fully fake-tested; Properties 1-6 covered.
- Objective outcome assertions work judge-free; judge is off by
  default and failure-isolated.
- `todo_*` feature shipped and driven green by its e2e scenario.
- Seed scenarios (reminder / news / todo) run; the news case judges
  *quality*.
- Standard test/lint/typecheck cycle green in both packages.

## Notes

- **Objective first, judge second.** The "did FITT actually do it"
  check is deterministic (read the store); the frontier judge only
  scores the fuzzy reply quality. Don't LLM-judge what an assertion
  covers.
- **Dev-driver, not a CI gate.** Live runs are non-deterministic and
  need the tunnel + kiro-cli; the CI-safe part is the fake-backed core
  + the stubbed-app smoke.
- **Extends `scenario_eval`** (real-model driver, multi-sample,
  transient handling) — don't fork it.
- **Judge is eval-only** — a frontier model here never becomes a
  runtime router target.
- Spec folder is descriptive (not a roadmap phase), matching
  chess-coach's `game-coaching-eval` precedent for a cross-cutting
  test-infra spec.
