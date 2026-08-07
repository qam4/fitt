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

- [x] 4. `gateway/src/gateway/e2e_judge.py`: `CliJudge` (headless
  command, stdin prompt → stdout verdict; temp 0 lives in the operator's
  command flags), `build_judge_prompt` (intent + rubric + reply +
  tool_sequence + objective outcome), and `parse_verdict` (JSON
  `{passed, score, reasoning}`, tolerating fences/prose, + a PASS/FAIL
  lenient fallback). Runner error / timeout / unparseable → un-judged
  verdict. Runner injected for testing. (R4.1, R4.2, R4.3; Design D3)
  DONE 2026-07-02.
- [x] 5. Tests: clean/fenced JSON parse, lenient PASS/FAIL, unparseable
  → raises; CliJudge with a fake runner (canned → parsed; raising →
  un-judged; garbage → un-judged); empty command rejected. No real
  kiro-cli. DONE 2026-07-02: 9 tests, ruff/mypy clean.

## Phase C — Driver

- [x] 6. Dispatch through the real chat pipeline. DONE 2026-07-02:
  `e2e_driver.build_http_dispatch` sends each turn as a chat request
  over the in-process ASGI transport against the DUT alias — full
  pipeline (memory injection + tool loop + persistence + the async
  indexer, drained between turns for multi-turn recall). `tool_sequence`
  recovered from the persisted turn (`_tools_from_last_turn`). Chose the
  HTTP path over `scenario_eval`'s `run_agent_loop` because the latter
  skips memory injection + persistence (fatal for the memory scenario).
  Tested against the Phase 4.6 stubbed app (2 e2e tests). Cassette +
  live DUT wiring fold into the CLI (task 8) / live run (Phase F).
- [x] 7. `snapshot`: read cron jobs (`app.state.cron`), `todos.md`,
  and recent event kinds into a plain dict at run end. (R3.1) DONE
  2026-07-02: `e2e_driver.snapshot_app` (+ `cron_at_ts_matches` /
  `todos_contain` assertion helpers); tested against a real in-process
  app with a seeded cron. 3 tests.
- [ ] 8. Guards + mechanics: refuse DUT==judge (P6); `--judge` off by
  default; `--samples`, `--out`, `--judge-command` configurable; ensure
  the tunnel before a live run. (R4.2, R4.3, R5, R6)
  - Tunnel-ensure helper DONE 2026-07-02 (`tunnel.py`, `ensure_tunnel`
    + `FITT_TUNNEL_CMD`): reachability check + optional detached start
    of an operator-configured command (kept out of the repo — shareable);
    the driver calls it before a live run (already-up / started /
    failed / no-cmd). 6 tests, fakes only.

## Phase D — Seed scenarios

- [x] 9. `e2e_scenarios.py` — **reminder**: assert a future one-shot
  (`at`) cron mentioning the subject within ~36h (tz-robust; the judge
  rubric checks the precise "9am tomorrow"). (R7.1, R3.2) DONE
  2026-07-02.
- [x] 10. **news_summary**: objective = web_search fired + a
  substantive (>80-char) reply; rubric judges quality (grounded,
  on-topic, not a refusal). (R7.1, R4.4) DONE 2026-07-02.
- [x] 11. **memory_recall** (Phase 9): multi-turn (state a fact, then
  ask); objective = `memory_search` in the tool_sequence AND the
  recalled keyword surfaces in the reply; rubric judges groundedness.
  Closes Phase 9's one unproven link. (R7.1) DONE 2026-07-02.
- [x] 12. Tests: each scenario's `outcome_assert` passes on a crafted
  good trajectory and fails on bad ones (past/wrong-subject cron,
  no-search, short reply, no-memory_search, fact-not-recalled). DONE
  2026-07-02: `test_e2e_scenarios.py`, 10 tests.

## Phase E — Todo feature, test-first

- [x] 13. **todo** scenario + `outcome_assert` = `todos.md` gained the
  item; write it FIRST (fails — no feature yet). (R7.1) DONE 2026-08-07:
  `todo_scenario` + `_todo_assert` in `e2e_scenarios.py`; asserts
  `snapshot["todos_text"]` contains the item.
- [x] 14. Build the `todo_*` tool group + markdown store
  (`$FITT_HOME/todos.md`), modeled on Phase 5 `learn_*`
  (`todo_add`/`todo_list`/`todo_done`/`todo_remove`); register in the
  core registry; wire onto `ToolContext`. Iterate until the objective
  assertion passes. DONE 2026-08-07: `todos.py` (`TodoStore` +
  `Todo`, checkbox format, mtime reload, ceiling), `tools/todo_tools.py`
  (add/list/done auto, remove ask); registered unconditionally in
  `build_core_tool_registry`; `ToolContext.todos` wired in `create_app`,
  both chat.py sites, and the cron runner.
- [x] 15. Unit tests for the todo store + tools (mirror the lessons
  tests). DONE 2026-08-07: `test_todos.py` (30) + `test_todo_tools.py`
  (17) + a todo scenario assertion in `test_e2e_scenarios.py`. Full
  gateway + telegram-bot suites green (one pre-existing log_bodies
  ordering flake, unrelated).

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
