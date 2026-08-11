"""Judged-e2e harness — pure core tests (Phase A).

Driven entirely by fakes (fake dispatch, fake snapshot, fake judge) —
no live model, no kiro-cli — so they're fast and CI-safe. Cover
Properties 1-6.
"""

from __future__ import annotations

from typing import Any

import pytest

from gateway.e2e_eval import (
    E2ETrajectory,
    JudgeInput,
    JudgeVerdict,
    OutcomeResult,
    RunResult,
    SelfJudgeError,
    TaskScenario,
    aggregate,
    ensure_distinct_judge,
    run_scenario,
)


def _dispatch(reply: str, tools: tuple[str, ...] = ()) -> Any:
    async def _fn(turns: list[dict[str, Any]]) -> RunResult:
        _fn.seen = turns  # type: ignore[attr-defined]
        return RunResult(reply=reply, tool_sequence=tools)

    return _fn


def _snapshot(state: dict[str, Any]) -> Any:
    return lambda: dict(state)


def _scenario(
    *,
    name: str = "s",
    turns: list[dict[str, Any]] | None = None,
    assert_fn: Any = None,
    rubric: str = "",
    requires_tools: tuple[str, ...] = (),
) -> TaskScenario:
    return TaskScenario(
        name=name,
        turns=turns or [{"role": "user", "content": "hi"}],
        outcome_assert=assert_fn or (lambda t: OutcomeResult(True, "ok")),
        rubric=rubric,
        requires_tools=requires_tools,
    )


# --------------------------------------------------------------- Property 1


async def test_run_captures_reply_tools_snapshot() -> None:
    scen = _scenario(assert_fn=lambda t: OutcomeResult("k" in t.snapshot, "checked"))
    res = await run_scenario(
        scen,
        dispatch=_dispatch("done", ("cron_add:ok",)),
        snapshot=_snapshot({"k": "v"}),
    )
    assert res.trajectory.run.reply == "done"
    assert res.trajectory.run.tool_sequence == ("cron_add:ok",)
    assert res.trajectory.snapshot == {"k": "v"}
    assert res.outcome.passed


async def test_multi_turn_sent_in_order() -> None:
    turns = [
        {"role": "user", "content": "one"},
        {"role": "user", "content": "two"},
    ]
    disp = _dispatch("ok")
    await run_scenario(_scenario(turns=turns), dispatch=disp, snapshot=_snapshot({}))
    assert [t["content"] for t in disp.seen] == ["one", "two"]  # type: ignore[attr-defined]


# --------------------------------------------------------------- Property 2


def test_trajectory_round_trip() -> None:
    traj = E2ETrajectory(
        scenario="s",
        turns=[{"role": "user", "content": "hi"}],
        run=RunResult(reply="r", tool_sequence=("t:ok",), loop_status="ok"),
        snapshot={"cron": [{"at_ts": 123}]},
    )
    assert E2ETrajectory.from_dict(traj.to_dict()) == traj


# --------------------------------------------------------------- Property 3


async def test_judge_off_yields_unjudged_but_complete() -> None:
    res = await run_scenario(
        _scenario(rubric="judge me"), dispatch=_dispatch("ok"), snapshot=_snapshot({})
    )
    assert res.outcome.passed  # objective still ran
    assert res.verdict.judged is False


async def test_judge_error_is_isolated() -> None:
    async def _boom(ji: JudgeInput) -> JudgeVerdict:
        raise RuntimeError("judge exploded")

    res = await run_scenario(
        _scenario(rubric="judge me"),
        dispatch=_dispatch("ok"),
        snapshot=_snapshot({}),
        judge=_boom,
    )
    # Not aborted — recorded as un-judged.
    assert res.verdict.judged is False
    assert "judge error" in res.verdict.reasoning
    assert res.outcome.passed


async def test_judge_runs_when_rubric_and_judge_present() -> None:
    async def _judge(ji: JudgeInput) -> JudgeVerdict:
        return JudgeVerdict(passed=True, score=0.9, reasoning="good")

    res = await run_scenario(
        _scenario(rubric="is it good?"),
        dispatch=_dispatch("a fine reply"),
        snapshot=_snapshot({}),
        judge=_judge,
    )
    assert res.verdict.judged is True
    assert res.verdict.passed
    assert res.verdict.score == 0.9


# --------------------------------------------------------------- Property 4


async def test_outcome_assertion_runs_without_judge() -> None:
    seen: dict[str, Any] = {}

    def _assert(traj: E2ETrajectory) -> OutcomeResult:
        seen["snapshot"] = traj.snapshot
        return OutcomeResult(traj.snapshot.get("cron_count", 0) == 1, "one cron expected")

    res = await run_scenario(
        _scenario(assert_fn=_assert),
        dispatch=_dispatch("ok"),
        snapshot=_snapshot({"cron_count": 1}),
    )
    assert res.outcome.passed
    assert seen["snapshot"] == {"cron_count": 1}  # ran with no LLM in the path


async def test_outcome_assertion_exception_becomes_fail() -> None:
    def _bad(traj: E2ETrajectory) -> OutcomeResult:
        raise KeyError("missing")

    res = await run_scenario(
        _scenario(assert_fn=_bad), dispatch=_dispatch("ok"), snapshot=_snapshot({})
    )
    assert res.outcome.passed is False
    assert "raised" in res.outcome.reason


# --------------------------------------------------------------- Property 5


async def test_aggregate_separates_objective_and_judge_rates() -> None:
    async def _judge_pass(ji: JudgeInput) -> JudgeVerdict:
        return JudgeVerdict(True, 1.0, "ok")

    async def _judge_fail(ji: JudgeInput) -> JudgeVerdict:
        return JudgeVerdict(False, 0.0, "bad")

    # obj pass + judge fail
    r1 = await run_scenario(
        _scenario(name="a", rubric="r"),
        dispatch=_dispatch("x"),
        snapshot=_snapshot({}),
        judge=_judge_fail,
    )
    # obj fail + judge pass
    r2 = await run_scenario(
        _scenario(name="b", assert_fn=lambda t: OutcomeResult(False, "nope"), rubric="r"),
        dispatch=_dispatch("y"),
        snapshot=_snapshot({}),
        judge=_judge_pass,
    )
    rep = aggregate([r1, r2])
    assert rep.total == 2
    assert rep.objective_passed == 1  # only r1
    assert rep.judged == 2
    assert rep.judge_passed == 1  # only r2
    assert rep.objective_rate == 0.5
    assert rep.judge_rate == 0.5
    assert "objective=" in rep.render()


def test_aggregate_judge_rate_none_when_unjudged() -> None:
    from gateway.e2e_eval import E2EResult

    traj = E2ETrajectory(scenario="s", turns=[], run=RunResult(reply="r"))
    res = E2EResult(
        scenario="s",
        trajectory=traj,
        outcome=OutcomeResult(True, "ok"),
        verdict=JudgeVerdict.unjudged("off"),
    )
    rep = aggregate([res])
    assert rep.judged == 0
    assert rep.judge_rate is None


# --------------------------------------------------------------- Property 6


def test_ensure_distinct_judge_rejects_self_judging() -> None:
    with pytest.raises(SelfJudgeError):
        ensure_distinct_judge("fitt-ec2-qwen3", "fitt-ec2-qwen3")
    # distinct is fine
    ensure_distinct_judge("fitt-ec2-qwen3", "fitt-smart")


# ------------------------------------------- unsupported scenarios
#
# Regression guard for the memory_recall episode: memory_search is only
# registered when memory.embedding_alias is configured, so on a
# retrieval-off deployment the scenario reported "memory_search did not
# fire" — identical to a model that had the tool and ignored it. Three
# models were graded down for a missing feature, and the judge agreed
# each time. A missing prerequisite must never look like a model result.


async def test_missing_required_tool_is_not_dispatched() -> None:
    dispatched: list[bool] = []

    async def _dispatch_fn(turns: list[dict[str, Any]]) -> RunResult:
        dispatched.append(True)
        return RunResult(reply="should never run")

    res = await run_scenario(
        _scenario(requires_tools=("memory_search",)),
        dispatch=_dispatch_fn,
        snapshot=_snapshot({}),
        available_tools=["todo_add", "cron_add"],
    )

    assert not dispatched, "a scenario with an unmet prerequisite still called the model"
    assert res.unsupported is not None
    assert "memory_search" in res.unsupported


async def test_unsupported_scenario_is_never_judged() -> None:
    judged: list[bool] = []

    async def _judge(ji: JudgeInput) -> JudgeVerdict:
        judged.append(True)
        return JudgeVerdict(True, 1.0, "ok")

    res = await run_scenario(
        _scenario(rubric="grade me", requires_tools=("memory_search",)),
        dispatch=_dispatch("x"),
        snapshot=_snapshot({}),
        judge=_judge,
        available_tools=[],
    )

    assert not judged, "the judge was asked to grade a scenario that never ran"
    assert not res.verdict.judged


async def test_unsupported_scenarios_are_excluded_from_both_rates() -> None:
    async def _judge(ji: JudgeInput) -> JudgeVerdict:
        return JudgeVerdict(True, 1.0, "ok")

    ran = await run_scenario(
        _scenario(name="todo", rubric="r"),
        dispatch=_dispatch("added"),
        snapshot=_snapshot({}),
        judge=_judge,
        available_tools=["todo_add"],
    )
    skipped = await run_scenario(
        _scenario(name="memory_recall", rubric="r", requires_tools=("memory_search",)),
        dispatch=_dispatch("x"),
        snapshot=_snapshot({}),
        judge=_judge,
        available_tools=["todo_add"],
    )

    rep = aggregate([ran, skipped])

    # 1/1, not 1/2: the deployment gap must not dilute the model's score.
    assert rep.total == 1
    assert rep.objective_passed == 1
    assert rep.objective_rate == 1.0
    assert rep.judged == 1
    assert [r.scenario for r in rep.unsupported] == ["memory_recall"]


async def test_report_calls_out_unsupported_scenarios() -> None:
    skipped = await run_scenario(
        _scenario(name="memory_recall", requires_tools=("memory_search",)),
        dispatch=_dispatch("x"),
        snapshot=_snapshot({}),
        available_tools=[],
    )

    rendered = aggregate([skipped]).render()

    assert "SKIPPED" in rendered
    assert "memory_search" in rendered
    # It must not read as a model verdict.
    assert "objective=FAIL" not in rendered


async def test_requirements_unchecked_when_registry_unknown() -> None:
    """Unit tests pass fake dispatches with no registry; don't guess."""
    res = await run_scenario(
        _scenario(requires_tools=("memory_search",)),
        dispatch=_dispatch("ran anyway"),
        snapshot=_snapshot({}),
    )

    assert res.unsupported is None
    assert res.trajectory.run.reply == "ran anyway"


# ------------------------------------------- inconclusive scenarios
#
# Regression guard for the cross-session recall episode: the model stored
# the fact with learn_add, lessons reach every system prompt regardless
# of session, so the recall turn answered correctly without retrieval.
# Scored as a failure, the judge accused a correct model of hallucinating
# a 1-in-10,000 number. A run that didn't exercise the thing under test
# must be reported as such, not graded.


async def test_inconclusive_outcome_is_never_judged() -> None:
    judged: list[bool] = []

    async def _judge(ji: JudgeInput) -> JudgeVerdict:
        judged.append(True)
        return JudgeVerdict(False, 0.0, "hallucinated")

    res = await run_scenario(
        _scenario(
            rubric="grade me",
            assert_fn=lambda t: OutcomeResult(False, "a lesson leaked it", inconclusive=True),
        ),
        dispatch=_dispatch("4821"),
        snapshot=_snapshot({}),
        judge=_judge,
    )

    assert not judged, "the judge graded a run that didn't test what it claims to"
    assert res.inconclusive == "a lesson leaked it"
    assert not res.verdict.judged


async def test_inconclusive_is_excluded_from_the_rates() -> None:
    ok = await run_scenario(
        _scenario(name="todo"), dispatch=_dispatch("added"), snapshot=_snapshot({})
    )
    undecided = await run_scenario(
        _scenario(
            name="memory_recall_cross_session",
            assert_fn=lambda t: OutcomeResult(False, "lesson leaked", inconclusive=True),
        ),
        dispatch=_dispatch("4821"),
        snapshot=_snapshot({}),
    )

    rep = aggregate([ok, undecided])

    assert rep.total == 1  # not 2
    assert rep.objective_rate == 1.0
    assert [r.scenario for r in rep.inconclusive] == ["memory_recall_cross_session"]


async def test_report_marks_inconclusive_distinctly_from_failure() -> None:
    undecided = await run_scenario(
        _scenario(
            name="memory_recall_cross_session",
            assert_fn=lambda t: OutcomeResult(False, "lesson leaked", inconclusive=True),
        ),
        dispatch=_dispatch("4821"),
        snapshot=_snapshot({}),
    )

    rendered = aggregate([undecided]).render()

    assert "INCONCLUSIVE" in rendered
    assert "objective=FAIL" not in rendered
