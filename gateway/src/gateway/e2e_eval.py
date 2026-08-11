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

from collections.abc import Awaitable, Callable, Collection
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
    timeline: tuple[dict[str, Any], ...] = ()  # Tier 2: per-iteration turn events
    loop_status: str = "ok"
    error: str | None = None
    earlier_tool_calls: tuple[dict[str, Any], ...] = ()
    """Tool calls from the scenario's *earlier* turns (``tool_calls``
    covers the graded final turn only).

    Needed because a side effect from turn 1 can change what turn 2 is
    even testing: a ``learn_add`` in the first session writes a global
    lesson that reaches every later system prompt, so a cross-session
    recall run can answer correctly without touching the retrieval
    index. Without visibility of the earlier turn, that's
    indistinguishable from the model inventing the answer."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply": self.reply,
            "tool_sequence": list(self.tool_sequence),
            "tool_calls": [dict(c) for c in self.tool_calls],
            "timeline": [dict(e) for e in self.timeline],
            "loop_status": self.loop_status,
            "error": self.error,
            "earlier_tool_calls": [dict(c) for c in self.earlier_tool_calls],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RunResult:
        return cls(
            reply=d["reply"],
            tool_sequence=tuple(d.get("tool_sequence", [])),
            tool_calls=tuple(d.get("tool_calls", [])),
            timeline=tuple(d.get("timeline", [])),
            loop_status=d.get("loop_status", "ok"),
            error=d.get("error"),
            earlier_tool_calls=tuple(d.get("earlier_tool_calls", [])),
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
    inconclusive: bool = False
    """The run didn't exercise what the scenario is about, so there's
    nothing to score — distinct from the model getting it wrong.

    Earned the hard way: the cross-session recall scenario answered
    correctly with no retrieval call, because the model had stored the
    fact as a *lesson* in the first session and lessons are injected
    into every system prompt regardless of session. FITT has three
    recall channels (session history, global lessons, retrieval index);
    a run that reached the answer through the wrong one tells us nothing
    about the one under test. Reported as inconclusive, excluded from
    the rates, and never sent to the judge — which had confidently
    accused the model of hallucinating a 1-in-10,000 number."""


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
    timeline: tuple[dict[str, Any], ...] = ()
    """Tier 2: the per-iteration turn timeline (LLM calls with
    finish_reason/tokens, planned vs executed tool calls, approvals).
    Empty at Tier 1. Included in the judge prompt only when the operator
    asks for the deeper detail level — it's the evidence needed to
    diagnose *why* a loop misbehaved, at the cost of prompt size."""


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
    requires_tools: tuple[str, ...] = ()
    """Tools that must be registered for this scenario to be *possible*.

    Without this, a scenario whose feature is switched off scores as a
    model failure. That is exactly what happened to ``memory_recall``:
    ``memory_search`` is only registered when ``memory.embedding_alias``
    is configured, so on a retrieval-off deployment the objective check
    reported "memory_search did not fire" — indistinguishable from a
    model that had the tool and ignored it — and the judge blamed the
    model, on three different models in a row. Declaring the
    prerequisite lets the runner report *unsupported* instead of
    grading."""

    requires_hint: str = ""
    """How an operator makes the missing tool exist. Appended to the
    unsupported reason, per Principle 8: when a capability is missing,
    say what's missing AND how to add it."""


@dataclass(frozen=True)
class E2EResult:
    """One scenario run: objective outcome (always) + judge verdict
    (``judged=False`` when off/errored)."""

    scenario: str
    trajectory: E2ETrajectory
    outcome: OutcomeResult
    verdict: JudgeVerdict
    unsupported: str | None = None
    """Set when the scenario never ran because a required tool is
    missing. Such a result is excluded from both pass-rates: it says
    something about the *deployment*, nothing about the model."""

    inconclusive: str | None = None
    """Set when the scenario ran but didn't exercise what it tests (see
    :attr:`OutcomeResult.inconclusive`). Also excluded from the rates
    and from judging."""

    @property
    def scored(self) -> bool:
        """Whether this result says anything about the model."""
        return self.unsupported is None and self.inconclusive is None


# --------------------------------------------------------------- run


async def run_scenario(
    scenario: TaskScenario,
    *,
    dispatch: DispatchFn,
    snapshot: SnapshotFn,
    judge: JudgeFn | None = None,
    judge_timeline: bool = False,
    available_tools: Collection[str] | None = None,
) -> E2EResult:
    """Run one scenario end to end and grade it.

    Sends the turns via ``dispatch``, snapshots the end state, runs the
    objective ``outcome_assert``, and — only when a ``judge`` is given
    AND the scenario has a rubric — scores the reply. Never raises: an
    assertion error becomes an objective fail; a judge error becomes an
    un-judged verdict (Properties 3, 4).

    When ``available_tools`` is supplied and the scenario declares
    ``requires_tools`` that aren't in it, the scenario is *not run*: it
    returns an unsupported result, unscored and unjudged. Grading a
    model on a tool it was never offered produces a confident wrong
    answer, which is worse than no answer."""
    missing = _missing_tools(scenario, available_tools)
    if missing:
        reason = (
            f"scenario needs {', '.join(missing)}, which "
            f"{'is' if len(missing) == 1 else 'are'} not registered on this "
            "deployment — not run, not scored"
        )
        if scenario.requires_hint:
            reason = f"{reason}. To enable: {scenario.requires_hint}"
        return E2EResult(
            scenario=scenario.name,
            trajectory=E2ETrajectory(
                scenario=scenario.name,
                turns=list(scenario.turns),
                run=RunResult(reply="", loop_status="not_run"),
            ),
            outcome=OutcomeResult(passed=False, reason=reason),
            verdict=JudgeVerdict.unjudged(reason),
            unsupported=reason,
        )

    run = await dispatch(scenario.turns)
    snap = snapshot()
    traj = E2ETrajectory(scenario=scenario.name, turns=list(scenario.turns), run=run, snapshot=snap)

    try:
        outcome = scenario.outcome_assert(traj)
    except Exception as exc:
        outcome = OutcomeResult(passed=False, reason=f"outcome assertion raised: {exc}")

    if outcome.inconclusive:
        # Nothing to grade: the run didn't exercise the thing under
        # test. Judging it anyway is how a correct model gets called a
        # hallucinator.
        return E2EResult(
            scenario=scenario.name,
            trajectory=traj,
            outcome=outcome,
            verdict=JudgeVerdict.unjudged(outcome.reason),
            inconclusive=outcome.reason,
        )

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
            timeline=run.timeline if judge_timeline else (),
        )
        try:
            verdict = await judge(judge_input)
        except Exception as exc:
            verdict = JudgeVerdict.unjudged(f"judge error: {exc}")
    else:
        verdict = JudgeVerdict.unjudged("judging disabled" if judge is None else "no rubric")

    return E2EResult(scenario=scenario.name, trajectory=traj, outcome=outcome, verdict=verdict)


def _missing_tools(
    scenario: TaskScenario, available_tools: Collection[str] | None
) -> tuple[str, ...]:
    """Required tools absent from the registry.

    ``None`` means the caller didn't tell us what's registered (unit
    tests with fake dispatches), so we can't check and don't guess."""
    if available_tools is None or not scenario.requires_tools:
        return ()
    have = set(available_tools)
    return tuple(t for t in scenario.requires_tools if t not in have)


# --------------------------------------------------------------- aggregate


@dataclass(frozen=True)
class E2EReport:
    """Objective and judge pass-rates computed *separately* — a run can
    pass the objective check but fail the judge, and vice versa."""

    total: int
    """Scenarios actually run. Unsupported ones are excluded, so a
    pass-rate is never diluted by a scenario the deployment can't
    attempt."""

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

    @property
    def unsupported(self) -> list[E2EResult]:
        return [r for r in self.results if r.unsupported]

    @property
    def inconclusive(self) -> list[E2EResult]:
        return [r for r in self.results if r.inconclusive]

    def render(self) -> str:
        jr = f"{self.judge_rate * 100:.0f}%" if self.judge_rate is not None else "n/a"
        lines = [
            f"Objective: {self.objective_passed}/{self.total} passed "
            f"({self.objective_rate * 100:.0f}%)",
            f"Judge: {self.judge_passed}/{self.judged} passed ({jr})"
            + ("" if self.judged else "  [judging off / no rubric]"),
        ]
        skipped = self.unsupported
        if skipped:
            names = ", ".join(r.scenario for r in skipped)
            lines.append(
                f"Unsupported: {len(skipped)} not run and excluded from both "
                f"rates ({names}) — a deployment gap, not a model result"
            )
        undecided = self.inconclusive
        if undecided:
            names = ", ".join(r.scenario for r in undecided)
            lines.append(
                f"Inconclusive: {len(undecided)} ran but didn't exercise what "
                f"they test, excluded from both rates ({names})"
            )
        for r in self.results:
            if r.unsupported:
                lines.append(f"  - {r.scenario}: SKIPPED  ({r.unsupported})")
                continue
            if r.inconclusive:
                lines.append(f"  - {r.scenario}: INCONCLUSIVE  ({r.inconclusive})")
                continue
            obj = "PASS" if r.outcome.passed else "FAIL"
            if not r.verdict.judged:
                jv = "unjudged"
            else:
                jv = "PASS" if r.verdict.passed else "FAIL"
            lines.append(f"  - {r.scenario}: objective={obj} judge={jv}  ({r.outcome.reason})")
        return "\n".join(lines)


def aggregate(results: list[E2EResult]) -> E2EReport:
    """Fold scenario results into a report, tracking objective and judge
    pass-rates independently (Property 5).

    Unsupported and inconclusive scenarios are kept in ``results`` (so
    the report can show them) but excluded from ``total`` and from the
    judge counts: neither measures the model."""
    scored = [r for r in results if r.scored]
    judged = [r for r in scored if r.verdict.judged]
    return E2EReport(
        total=len(scored),
        objective_passed=sum(1 for r in scored if r.outcome.passed),
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
