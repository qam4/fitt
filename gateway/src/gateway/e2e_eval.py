"""Judged end-to-end harness — pure core (Phase A).

Drive a task scenario through an injected ``dispatch`` (the real
pipeline in the driver, a fake in tests), capture a serializable
:class:`E2ETrajectory` (reply + tool_sequence + a side-effect
snapshot = ground truth), then grade it in two separable passes:

* **objective** — a deterministic ``outcome_assert`` reads the real end
  state and returns pass/fail ("did FITT actually do it"). No LLM.
* **judge** — an *optional* frontier judge scores the fuzzy reply
  quality against the scenario's rubric. Off by default; a judge error
  yields an *un-judged* verdict, never an aborted run.

Pure and I/O-free: ``dispatch`` / ``snapshot`` / ``judge`` are injected
callables, so the loop, the trajectory model, and the aggregation are
unit-testable with fakes (no live model, no kiro-cli). The driver
(Phase C) wires the real implementations. Modeled on chess-coach
``eval/game_coaching.py``; see ``.kiro/specs/judged-e2e-harness/``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------- ports

# turns -> the model's run result (reply + what tools fired).
DispatchFn = Callable[[list[dict[str, Any]]], Awaitable["RunResult"]]
# () -> a plain-dict snapshot of the relevant stores at run end (ground truth).
SnapshotFn = Callable[[], dict[str, Any]]
# a trajectory -> objective pass/fail. Deterministic, no LLM.
OutcomeAssert = Callable[["E2ETrajectory"], "OutcomeResult"]
# judge input -> a verdict. Async (may shell out / call a model).
JudgeFn = Callable[["JudgeInput"], Awaitable["JudgeVerdict"]]


# --------------------------------------------------------------- value types


@dataclass(frozen=True)
class RunResult:
    """What one dispatch of a scenario's turns produced."""

    reply: str
    tool_sequence: tuple[str, ...] = ()  # "<tool>:<result_status>" per call
    tool_calls: tuple[dict[str, Any], ...] = ()  # {name, args, ok, result} per call
    loop_status: str = "ok"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply": self.reply,
            "tool_sequence": list(self.tool_sequence),
            "tool_calls": [dict(c) for c in self.tool_calls],
            "loop_status": self.loop_status,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RunResult:
        return cls(
            reply=d["reply"],
            tool_sequence=tuple(d.get("tool_sequence", [])),
            tool_calls=tuple(d.get("tool_calls", [])),
            loop_status=d.get("loop_status", "ok"),
            error=d.get("error"),
        )


@dataclass(frozen=True)
class E2ETrajectory:
    """A run reduced to its replayable, judgeable record."""

    scenario: str
    turns: list[dict[str, Any]]
    run: RunResult
    snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "turns": list(self.turns),
            "run": self.run.to_dict(),
            "snapshot": dict(self.snapshot),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> E2ETrajectory:
        return cls(
            scenario=d["scenario"],
            turns=list(d.get("turns", [])),
            run=RunResult.from_dict(d["run"]),
            snapshot=dict(d.get("snapshot", {})),
        )


@dataclass(frozen=True)
class OutcomeResult:
    """Objective, judge-free verdict on the real end state."""

    passed: bool
    reason: str


@dataclass(frozen=True)
class JudgeInput:
    """What the frontier judge sees for one scenario.

    Beyond the user-facing ``reply``, the judge is handed the system
    *internals* — the tools that actually executed and the resulting
    side-effect ``snapshot`` (cron jobs, todos, recent events) — so it
    can grade the reply against what really happened, not just what the
    reply claims. This is the chess-coach pattern: ground the judge in
    the objective record."""

    intent: str
    rubric: str
    reply: str
    tool_sequence: tuple[str, ...]
    outcome_passed: bool
    outcome_reason: str
    snapshot: dict[str, Any] = field(default_factory=dict)
    tool_calls: tuple[dict[str, Any], ...] = ()  # {name, args, ok, result} per call
    loop_status: str = "ok"
    error: str | None = None


@dataclass(frozen=True)
class JudgeVerdict:
    """The judge's result. ``judged=False`` means the reply was not
    judged (judge off, no rubric, or a judge/parse error)."""

    passed: bool
    score: float | None
    reasoning: str
    judged: bool = True

    @classmethod
    def unjudged(cls, reason: str) -> JudgeVerdict:
        return cls(passed=False, score=None, reasoning=reason, judged=False)


@dataclass(frozen=True)
class TaskScenario:
    """A natural-language request + how to grade it.

    ``outcome_assert`` is the objective (always-run) check; ``rubric``
    (when non-empty and a judge is supplied) drives the optional fuzzy
    quality judgement of the reply."""

    name: str
    turns: list[dict[str, Any]]
    outcome_assert: OutcomeAssert
    rubric: str = ""


@dataclass(frozen=True)
class E2EResult:
    """One scenario run: objective outcome (always) + judge verdict
    (``judged=False`` when off/errored)."""

    scenario: str
    trajectory: E2ETrajectory
    outcome: OutcomeResult
    verdict: JudgeVerdict


# --------------------------------------------------------------- run


async def run_scenario(
    scenario: TaskScenario,
    *,
    dispatch: DispatchFn,
    snapshot: SnapshotFn,
    judge: JudgeFn | None = None,
) -> E2EResult:
    """Run one scenario end to end and grade it.

    Sends the turns via ``dispatch``, snapshots the end state, runs the
    objective ``outcome_assert``, and — only when a ``judge`` is given
    AND the scenario has a rubric — scores the reply. Never raises: an
    assertion error becomes an objective fail; a judge error becomes an
    un-judged verdict (Properties 3, 4)."""
    run = await dispatch(scenario.turns)
    snap = snapshot()
    traj = E2ETrajectory(scenario=scenario.name, turns=list(scenario.turns), run=run, snapshot=snap)

    try:
        outcome = scenario.outcome_assert(traj)
    except Exception as exc:
        outcome = OutcomeResult(passed=False, reason=f"outcome assertion raised: {exc}")

    if judge is not None and scenario.rubric:
        judge_input = JudgeInput(
            intent=scenario.name,
            rubric=scenario.rubric,
            reply=run.reply,
            tool_sequence=run.tool_sequence,
            outcome_passed=outcome.passed,
            outcome_reason=outcome.reason,
            snapshot=traj.snapshot,
            tool_calls=run.tool_calls,
            loop_status=run.loop_status,
            error=run.error,
        )
        try:
            verdict = await judge(judge_input)
        except Exception as exc:
            verdict = JudgeVerdict.unjudged(f"judge error: {exc}")
    else:
        verdict = JudgeVerdict.unjudged("judging disabled" if judge is None else "no rubric")

    return E2EResult(scenario=scenario.name, trajectory=traj, outcome=outcome, verdict=verdict)


# --------------------------------------------------------------- aggregate


@dataclass(frozen=True)
class E2EReport:
    """Objective and judge pass-rates computed *separately* — a run can
    pass the objective check but fail the judge, and vice versa."""

    total: int
    objective_passed: int
    judged: int
    judge_passed: int
    results: list[E2EResult]

    @property
    def objective_rate(self) -> float:
        return self.objective_passed / self.total if self.total else 0.0

    @property
    def judge_rate(self) -> float | None:
        return self.judge_passed / self.judged if self.judged else None

    def render(self) -> str:
        jr = f"{self.judge_rate * 100:.0f}%" if self.judge_rate is not None else "n/a"
        lines = [
            f"Objective: {self.objective_passed}/{self.total} passed "
            f"({self.objective_rate * 100:.0f}%)",
            f"Judge: {self.judge_passed}/{self.judged} passed ({jr})"
            + ("" if self.judged else "  [judging off / no rubric]"),
        ]
        for r in self.results:
            obj = "PASS" if r.outcome.passed else "FAIL"
            if not r.verdict.judged:
                jv = "unjudged"
            else:
                jv = "PASS" if r.verdict.passed else "FAIL"
            lines.append(f"  - {r.scenario}: objective={obj} judge={jv}  ({r.outcome.reason})")
        return "\n".join(lines)


def aggregate(results: list[E2EResult]) -> E2EReport:
    """Fold scenario results into a report, tracking objective and judge
    pass-rates independently (Property 5)."""
    judged = [r for r in results if r.verdict.judged]
    return E2EReport(
        total=len(results),
        objective_passed=sum(1 for r in results if r.outcome.passed),
        judged=len(judged),
        judge_passed=sum(1 for r in judged if r.verdict.passed),
        results=results,
    )


# --------------------------------------------------------------- guard


class SelfJudgeError(ValueError):
    """Raised when the judge alias equals the DUT alias — a model must
    not grade its own homework (Property 6)."""


def ensure_distinct_judge(dut_alias: str, judge_alias: str) -> None:
    """The driver calls this before a judged run."""
    if dut_alias == judge_alias:
        raise SelfJudgeError(
            f"judge alias {judge_alias!r} must differ from the DUT alias "
            f"{dut_alias!r} — the model can't judge its own output"
        )
