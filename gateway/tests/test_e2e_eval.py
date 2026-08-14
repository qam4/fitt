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
    SetupContext,
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
    setup: Any = None,
    settle: Any = None,
) -> TaskScenario:
    return TaskScenario(
        name=name,
        turns=turns or [{"role": "user", "content": "hi"}],
        outcome_assert=assert_fn or (lambda t: OutcomeResult(True, "ok")),
        rubric=rubric,
        requires_tools=requires_tools,
        setup=setup,
        settle=settle,
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


# ------------------------------------------- setup hooks
#
# Some preconditions can't be created by the model without changing what
# the scenario measures: asking a model to remember a fact makes it call
# learn_add, whose global lessons reach every later system prompt, so a
# cross-session recall test never touches the retrieval index.


async def test_setup_runs_before_the_turns() -> None:
    order: list[str] = []

    async def _setup(ctx: SetupContext) -> None:
        order.append("setup")

    async def _dispatch_fn(turns: list[dict[str, Any]]) -> RunResult:
        order.append("dispatch")
        return RunResult(reply="ok")

    res = await run_scenario(
        _scenario(setup=_setup),
        dispatch=_dispatch_fn,
        snapshot=_snapshot({}),
        setup_context=SetupContext(app=object(), session_id="e2e-x-0"),
    )

    assert order == ["setup", "dispatch"]
    assert res.scored


async def test_setup_receives_the_run_session_id() -> None:
    seen: list[str] = []

    async def _setup(ctx: SetupContext) -> None:
        seen.append(ctx.session_id)

    await run_scenario(
        _scenario(setup=_setup),
        dispatch=_dispatch("ok"),
        snapshot=_snapshot({}),
        setup_context=SetupContext(app=object(), session_id="e2e-recall-0"),
    )

    # Hooks derive sibling sessions the same way the dispatch does.
    assert seen == ["e2e-recall-0"]


async def test_failing_setup_is_inconclusive_not_a_model_failure() -> None:
    dispatched: list[bool] = []

    async def _setup(ctx: SetupContext) -> None:
        raise RuntimeError("memory is disabled")

    async def _dispatch_fn(turns: list[dict[str, Any]]) -> RunResult:
        dispatched.append(True)
        return RunResult(reply="should never run")

    res = await run_scenario(
        _scenario(setup=_setup, rubric="grade me"),
        dispatch=_dispatch_fn,
        snapshot=_snapshot({}),
        setup_context=SetupContext(app=object(), session_id="e2e-x-0"),
    )

    assert not dispatched, "the turns ran on a precondition that never landed"
    assert res.inconclusive is not None
    assert "memory is disabled" in res.inconclusive
    assert not res.verdict.judged


async def test_missing_setup_context_is_inconclusive() -> None:
    """A runner that forgets to pass the context is a wiring bug, and
    must not read as a model result."""

    async def _setup(ctx: SetupContext) -> None:  # pragma: no cover - never called
        raise AssertionError("unreachable")

    res = await run_scenario(
        _scenario(setup=_setup), dispatch=_dispatch("ok"), snapshot=_snapshot({})
    )

    assert res.inconclusive is not None
    assert "setup context" in res.inconclusive


async def test_prerequisite_check_precedes_setup() -> None:
    """No point planting state for a scenario that can't run."""
    setup_ran: list[bool] = []

    async def _setup(ctx: SetupContext) -> None:
        setup_ran.append(True)

    res = await run_scenario(
        _scenario(setup=_setup, requires_tools=("memory_search",)),
        dispatch=_dispatch("ok"),
        snapshot=_snapshot({}),
        available_tools=[],
        setup_context=SetupContext(app=object(), session_id="e2e-x-0"),
    )

    assert not setup_ran
    assert res.unsupported is not None


# ------------------------------------------- settle hooks
#
# Not everything FITT does is a reply to a turn: a cron job fires on a
# scheduler tick, so "did the reminder actually fire?" needs the clock
# advanced between the turns and the snapshot. The alternative is
# sleeping, which is slow, flaky, and forbidden by the steering rules.


async def test_settle_runs_after_the_turns_and_before_the_snapshot() -> None:
    order: list[str] = []

    async def _dispatch_fn(turns: list[dict[str, Any]]) -> RunResult:
        order.append("dispatch")
        return RunResult(reply="ok")

    async def _settle(ctx: SetupContext) -> None:
        order.append("settle")

    def _snapshot_fn() -> dict[str, Any]:
        order.append("snapshot")
        return {}

    res = await run_scenario(
        _scenario(settle=_settle),
        dispatch=_dispatch_fn,
        snapshot=_snapshot_fn,
        setup_context=SetupContext(app=object(), session_id="e2e-x-0"),
    )

    assert order == ["dispatch", "settle", "snapshot"]
    assert res.scored


async def test_failing_settle_is_inconclusive_not_a_model_failure() -> None:
    async def _settle(ctx: SetupContext) -> None:
        raise RuntimeError("no cron_scheduler on app.state")

    res = await run_scenario(
        _scenario(settle=_settle, rubric="grade me"),
        dispatch=_dispatch("ok"),
        snapshot=_snapshot({}),
        setup_context=SetupContext(app=object(), session_id="e2e-x-0"),
    )

    assert res.inconclusive is not None
    assert "no cron_scheduler" in res.inconclusive
    assert not res.verdict.judged


async def test_setup_and_settle_both_run_in_order() -> None:
    order: list[str] = []

    async def _setup(ctx: SetupContext) -> None:
        order.append("setup")

    async def _settle(ctx: SetupContext) -> None:
        order.append("settle")

    async def _dispatch_fn(turns: list[dict[str, Any]]) -> RunResult:
        order.append("dispatch")
        return RunResult(reply="ok")

    await run_scenario(
        _scenario(setup=_setup, settle=_settle),
        dispatch=_dispatch_fn,
        snapshot=_snapshot({}),
        setup_context=SetupContext(app=object(), session_id="e2e-x-0"),
    )

    assert order == ["setup", "dispatch", "settle"]


# ------------------------------------------- objective/judge disagreement
#
# The two layers fail differently: the objective check can only be wrong
# about the *scenario*, the judge only about the *reply*. So a split is
# the report's sharpest signal, and it caught a real one —
# asks_before_acting scored objective=FAIL judge=PASS for a whole run
# because a tool description had grown a clause that resolved the very
# ambiguity the scenario was built on. Both layers were working; the
# scenario had gone stale. It was visible only by reading the
# per-scenario lines side by side, so promote it to a report field.


async def _judged(name: str, *, objective: bool, judge: bool) -> Any:
    async def _judge_fn(ji: JudgeInput) -> JudgeVerdict:
        return JudgeVerdict(judge, 1.0 if judge else 0.0, "graded")

    return await run_scenario(
        _scenario(
            name=name,
            rubric="r",
            assert_fn=lambda t: OutcomeResult(objective, "checked"),
        ),
        dispatch=_dispatch("a reply"),
        snapshot=_snapshot({}),
        judge=_judge_fn,
    )


async def test_disagreement_is_reported_when_the_judge_passes_a_failure() -> None:
    """The asks_before_acting shape: code says no, judge says yes."""
    split = await _judged("asks_before_acting", objective=False, judge=True)
    agreed = await _judged("todo", objective=True, judge=True)

    rep = aggregate([split, agreed])

    assert [r.scenario for r in rep.disagreements] == ["asks_before_acting"]


async def test_disagreement_is_reported_in_the_other_direction_too() -> None:
    """A side effect landing while the reply is nonsense is equally worth
    a look — that's the `<|tool_response>` case."""
    split = await _judged("reminder", objective=True, judge=False)

    assert [r.scenario for r in aggregate([split]).disagreements] == ["reminder"]


async def test_agreement_reports_no_disagreement() -> None:
    both_fail = await _judged("a", objective=False, judge=False)
    both_pass = await _judged("b", objective=True, judge=True)

    assert aggregate([both_fail, both_pass]).disagreements == []


async def test_unjudged_runs_cannot_disagree() -> None:
    """With judging off every scenario would otherwise look like a split
    against the judge's default-False verdict."""
    res = await run_scenario(
        _scenario(name="a", assert_fn=lambda t: OutcomeResult(True, "ok")),
        dispatch=_dispatch("ok"),
        snapshot=_snapshot({}),
    )

    assert aggregate([res]).disagreements == []


async def test_unscored_runs_cannot_disagree() -> None:
    """Unsupported and inconclusive results carry no model verdict, so
    their objective=False must not read as a split."""
    unsupported = await run_scenario(
        _scenario(name="memory_recall", rubric="r", requires_tools=("memory_search",)),
        dispatch=_dispatch("x"),
        snapshot=_snapshot({}),
        available_tools=[],
    )
    undecided = await run_scenario(
        _scenario(
            name="cross_session",
            rubric="r",
            assert_fn=lambda t: OutcomeResult(False, "lesson leaked", inconclusive=True),
        ),
        dispatch=_dispatch("4821"),
        snapshot=_snapshot({}),
    )

    assert aggregate([unsupported, undecided]).disagreements == []


async def test_report_names_the_split_and_which_way_it_went() -> None:
    split = await _judged("asks_before_acting", objective=False, judge=True)

    rendered = aggregate([split]).render()

    assert "Disagreements: 1" in rendered
    assert "asks_before_acting (objective=FAIL, judge=PASS)" in rendered
    assert "suspect the scenario" in rendered


# ------------------------------------------- the loop that ran
#
# Audit finding: the harness never pinned or recorded which agent loop ran.
# `is_orchestrated` is keyed on the alias, the command never set
# `cfg.orchestration`, so the loop came from whatever the operator's config
# happened to hold. Every recorded run was the flat loop and no artifact
# said so — a whole standing matrix of uninterpretable provenance.


def _sidecar(**kwargs: Any) -> dict[str, Any]:
    from gateway.e2e_eval import E2EResult, report_to_dict

    res = E2EResult(
        scenario="s",
        trajectory=E2ETrajectory(scenario="s", turns=[], run=RunResult(reply="r")),
        outcome=OutcomeResult(True, "ok"),
        verdict=JudgeVerdict.unjudged("off"),
    )
    return report_to_dict(aggregate([res]), dut="fitt-ec2-gemma4", **kwargs)


def test_sidecar_records_the_loop_mode() -> None:
    assert _sidecar(mode="planned")["mode"] == "planned"
    assert _sidecar(mode="flat")["mode"] == "flat"


def test_an_unrecorded_mode_is_labelled_not_assumed_flat() -> None:
    """Older sidecars were flat *in practice*, but nothing pinned it, so
    back-filling them as "flat" would assert something the run never
    established."""
    from gateway.e2e_eval import UNRECORDED_MODE

    assert _sidecar()["mode"] == UNRECORDED_MODE
    assert UNRECORDED_MODE != "flat"
