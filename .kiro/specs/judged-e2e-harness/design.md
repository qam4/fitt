# Judged End-to-End Harness: Design

## Overview

A pure, I/O-free core (task scenario → run → objective outcome check →
optional frontier judge → report) under a thin driver that wires the
real FITT pipeline, a real DUT model, and the kiro-cli judge. Mirrors
the chess-coach `game-coaching-eval` harness and extends FITT's own
`scenario_eval`: where `scenario_eval` grades a multi-step run with a
hand-written structural classifier, this grades it with (a) a
**deterministic outcome assertion** against the real end state and (b)
an **optional frontier judge** for reply quality.

The insight (from chess-coach): the run is a *generator* of a
trajectory carrying the reply + tool_sequence + a **side-effect
snapshot** (ground truth). Grading is then two cheap, separable passes
over that trajectory — one objective, one fuzzy — neither of which
re-runs the model.

## Architecture

```mermaid
flowchart TD
    CFG[run config: DUT alias, judge on/off, samples, out] --> DRV[driver: fitt eval e2e]
    SCN[TaskScenario: turns + outcome_assert + rubric?] --> DRV
    DRV -->|turns| DISP[real pipeline dispatch<br/>DUT alias via scenario_eval]
    DISP -->|reply + tool_sequence| TRAJ[E2ETrajectory]
    DRV -->|snapshot after run| SNAP[side-effect snapshot<br/>cron / todos / memory]
    SNAP --> TRAJ
    TRAJ --> ASSERT[outcome_assert<br/>objective, judge-free]
    TRAJ --> JUDGE[frontier judge<br/>kiro-cli + rubric  (optional)]
    ASSERT --> REP[E2EReport + JSON]
    JUDGE --> REP
```

Two grading layers, deliberately separable:
- **Objective (always):** `outcome_assert(traj) -> OutcomeResult` reads
  the real end state and returns pass/fail + reason. No LLM. This is
  the "did FITT get it done" layer.
- **Judge (optional, off by default):** a frontier model scores the
  fuzzy reply quality against the scenario's rubric.

## Components and interfaces

### Pure core — `gateway/src/gateway/e2e_eval.py`

Depends only on small injected callables + the value types, so it is
fully unit-testable with fakes (no live model, no kiro-cli).

```python
# Ports (injected) keep the core pure:
DispatchFn = Callable[[list[dict]], Awaitable[RunResult]]  # turns -> reply+tool_sequence
SnapshotFn = Callable[[], dict[str, Any]]                  # end-state side-effect snapshot
OutcomeAssert = Callable[["E2ETrajectory"], "OutcomeResult"]  # objective, deterministic
JudgeFn = Callable[["JudgeInput"], Awaitable["JudgeVerdict"]]  # frontier judge

@dataclass(frozen=True)
class RunResult:
    reply: str
    tool_sequence: tuple[str, ...]     # "<tool>:<result_status>" per call
    loop_status: str
    error: str | None = None

@dataclass(frozen=True)
class E2ETrajectory:
    scenario: str
    turns: list[dict[str, Any]]        # the user turns sent
    run: RunResult
    snapshot: dict[str, Any]           # cron/todos/memory ground truth at end
    def to_dict(self)/from_dict(...)

@dataclass(frozen=True)
class OutcomeResult:
    passed: bool
    reason: str

@dataclass(frozen=True)
class JudgeVerdict:
    passed: bool
    score: float | None
    reasoning: str
    judged: bool = True                # False => un-judged (judge off / errored)

@dataclass(frozen=True)
class E2EResult:
    scenario: str
    trajectory: E2ETrajectory
    outcome: OutcomeResult             # objective (always present)
    verdict: JudgeVerdict              # judged=False when off/errored

async def run_scenario(scenario, *, dispatch, snapshot, judge=None) -> E2EResult:
    """Send the turns via `dispatch`, capture the snapshot, run the
    objective `scenario.outcome_assert`, and (if `judge`) score reply
    quality. Never raises out of the judge — a judge error yields an
    un-judged verdict (Property 3)."""

def aggregate(results: list[E2EResult]) -> E2EReport: ...
```

`TaskScenario`:

```python
@dataclass(frozen=True)
class TaskScenario:
    name: str
    turns: list[dict[str, Any]]        # one or more user messages
    outcome_assert: OutcomeAssert      # objective end-state check
    rubric: str = ""                   # judge rubric; "" => reply not judged
```

### Judge provider — `gateway/src/gateway/e2e_judge.py`

`CliJudge`: headless kiro-cli, `stdin prompt -> stdout verdict`
(OD1-confirmed contract, modeled on chess-coach `CliProvider`). Builds
the judge prompt from `JudgeInput` (intent + rubric + reply +
tool_sequence + objective outcome), runs the command at temperature 0,
parses a structured verdict (JSON `{passed, score, reasoning}` with a
lenient fallback). A provider/parse error surfaces as an un-judged
verdict, never an exception into the run (Property 3). A cloud-alias
judge (via FITT's router) is a drop-in alternative implementing the
same `JudgeFn`.

The judge alias MUST differ from the DUT alias (Property 6) — the
driver refuses DUT==judge with a clear error.

### Driver — `fitt eval e2e` (extends the `eval` CLI group)

Wires the real components:
- `dispatch` = send the scenario turns through the real pipeline
  against the DUT alias, reusing `scenario_eval`'s real-model driver
  (and its transient-failure handling / tunnel-down skip). Optionally
  wrapped in a `RecordingRouter` for a replay cassette (R2.2).
- `snapshot` = read the relevant stores at run end — `app.state.cron`
  jobs, `todos.md`, memory/history, event log — into a plain dict.
- `judge` = `CliJudge` when `--judge` is set, else `None`.
- Runs each scenario (optionally k samples), aggregates, writes the
  trajectory JSON + report to `--out`. Safe under kiro-monitor.

### Scenarios — `gateway/src/gateway/e2e_scenarios.py`

Seed set (R7.1):
- **reminder**: turn "remind me to call the doctor tomorrow at 9am";
  `outcome_assert` = a one-shot cron exists with `at_ts` within
  tolerance of tomorrow 09:00 in the configured tz. Rubric optional
  (confirmation quality).
- **news_summary**: turn "give me a news summary about X"; objective
  layer is light (a substantive reply + a web_search in the
  tool_sequence); **rubric judges summary quality** (grounded,
  on-topic, substantive) — the motivating case (R4.4).
- **todo**: turn "add 'call the doctor' to my todos"; `outcome_assert`
  = `todos.md` gained the item. Drives the `todo_*` feature (built
  test-first against this scenario).

## Design decisions

- **D1. Objective layer is primary; judge is the fuzzy add-on, off by
  default.** Directly answers "verify it actually got done" — assert
  the real side effect deterministically; only judge what an assertion
  can't (reply quality). (R3, R4.3)
- **D2. Extend `scenario_eval`, don't fork it.** Reuse its real-model
  dispatch + transient handling + multi-sample; the new code is the
  side-effect snapshot, the objective assertion runner, the judge, and
  the scenario set. (R1.1)
- **D3. Judge = frontier via kiro-cli, out-classing the DUT; never the
  runtime.** DUT==judge is refused. (R4.2, Property 6)
- **D4. Pure core over injected ports.** Dispatch, snapshot, and judge
  are callables, so the loop/trajectory/assertion/aggregate are tested
  with fakes — no live deps in CI. (R6.1, R8.2)
- **D5. Replayable trajectory + cassette.** A failing run is
  re-inspectable and re-judgeable without re-running the model. (R2)
- **D6. Dev-driver, not a CI gate.** Non-deterministic; multi-sample
  for signal; documented, not hidden. (R5.2)

## Correctness properties

- **P1. Run faithfulness.** `run_scenario` over a fake dispatch
  produces a trajectory with the reply + tool_sequence + snapshot; a
  multi-turn scenario sends turns in order. (R1.1, R1.2)
- **P2. Trajectory round-trip.** `from_dict(to_dict(t)) == t`; a saved
  trajectory is judgeable without re-dispatch. (R2.1, R2.3)
- **P3. Judge-optional / failure isolation.** Judge `None` → complete
  objective result with `verdict.judged=False`; a judge error → an
  un-judged verdict, not an aborted run. (R3.4, R4.3)
- **P4. Objective independence.** `outcome_assert` runs and returns
  pass/fail with no LLM in the path. (R3.1)
- **P5. Aggregate correctness.** Objective pass-rate and judge
  pass-rate are computed *separately* over samples (a run can pass the
  objective check but fail the judge, and vice versa). (R7)
- **P6. No self-judging.** DUT alias == judge alias is rejected before
  any run. (R4.2)

## Error handling

- DUT dispatch transient failure (tunnel down, timeout) → recorded as a
  transient outcome, excluded from the capability denominator (reuse
  `scenario_eval` conventions); the harness warns cleanly (OD2).
- Objective assertion exception → the scenario result is a fail with
  the exception as the reason (never crashes the batch).
- Judge/parse error → un-judged verdict (P3).
- Bad config (DUT==judge, unknown alias) → fail fast at startup.

## Testing strategy

- **Pure core (fakes, no I/O):** P1-P6 with a fake `dispatch` (canned
  RunResult), a fake `snapshot`, per-scenario assertions over crafted
  snapshots, and a fake `judge` (canned + raising). Trajectory
  round-trip (+ property test). Aggregate math.
- **Judge adapter:** a fake Cli returning canned JSON → parsed verdict;
  a raising/garbage-output Cli → un-judged.
- **Smoke (CI-safe):** one scenario through the existing Phase 4.6 e2e
  app (stubbed LLM) + a fake judge — proves the driver wiring end to
  end without a live model. Separate from the live runs.
- **Live (manual / kiro-monitor):** reminder + news + todo against
  `fitt-ec2-qwen3` with the kiro-cli judge on.

## Sequencing

1. Pure core (`e2e_eval.py`): types + `run_scenario` + `aggregate`,
   judge-optional, fully fake-tested.
2. Judge provider (`e2e_judge.py`): `CliJudge` headless + parse +
   failure isolation, fake-tested.
3. Driver (`fitt eval e2e`): real dispatch (via `scenario_eval`) +
   side-effect snapshot + cassette; DUT==judge guard; tunnel-down skip.
4. Seed scenarios: reminder (cron assertion) + news (quality judge).
5. Todo **test-first**: todo scenario + assertion, then build the
   `todo_*` tools until the objective assertion passes.
6. Smoke + docs + first live run under kiro-monitor; record findings.

The pure core (1) has no live dependency — it lands and is proven
before any model/kiro-cli wiring, the same discipline the eval tools
and chess-coach follow.

## References

- chess-coach `game-coaching-eval` (`eval/game_coaching.py`,
  `scripts/eval_game_coaching.py`) — pure-core + driver, objective
  fidelity + optional CliProvider judge, judge-off-by-default.
- `scenario_eval.py` (real-model driver, multi-sample), `alias_eval.py`
  (conventions), `record_replay.py` (cassette), the Phase 4.6 e2e
  harness (stubbed-app smoke).
