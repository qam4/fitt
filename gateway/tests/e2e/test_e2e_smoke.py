"""Phase F task 16 — CI-safe smoke of the judged-e2e harness.

Runs the todo scenario end to end through the real chat pipeline with a
stubbed LLM + a fake judge, proving the full wiring — run_scenario ->
build_http_dispatch (real pipeline + tool execution + persistence) ->
snapshot_app -> outcome_assert -> judge -> aggregate — holds together
without a live model or kiro-cli. The live version is `fitt eval e2e`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gateway.app import create_app
from gateway.e2e_driver import build_http_dispatch, snapshot_app
from gateway.e2e_eval import JudgeInput, JudgeVerdict, aggregate, run_scenario
from gateway.e2e_scenarios import todo_scenario

from .._fixtures import PERSONAL_TOKEN, build_test_config
from .._llm_stubs import stub_reply, stub_tool_call
from .conftest import StubbedLLM


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    # FITT_HOME must be set before create_app so the todo store and the
    # snapshot both resolve to the same isolated todos.md.
    monkeypatch.setenv("FITT_HOME", str(tmp_path))
    monkeypatch.setenv("FITT_SKIP_SHELL_PROBE", "1")
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    return create_app(cfg)


async def test_todo_scenario_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stubbed_llm: StubbedLLM
) -> None:
    app = _app(tmp_path, monkeypatch)
    # The (stubbed) model calls the real todo_add tool, then confirms.
    stubbed_llm.load(
        [
            stub_tool_call("todo_add", {"text": "call the doctor"}),
            stub_reply("Added 'call the doctor' to your todo list."),
        ]
    )

    async def fake_judge(ji: JudgeInput) -> JudgeVerdict:
        # A frontier judge would look at ji.reply/rubric; the fake just
        # confirms the wiring reaches the judge with a populated input.
        assert "doctor" in ji.reply.lower()
        assert ji.outcome_passed
        return JudgeVerdict(passed=True, score=1.0, reasoning="reply confirms the add")

    scen = todo_scenario(item="call the doctor")
    dispatch = build_http_dispatch(
        app, alias="fitt-default", token=PERSONAL_TOKEN, session_id="main"
    )
    result = await run_scenario(
        scen,
        dispatch=dispatch,
        snapshot=lambda: snapshot_app(app),
        judge=fake_judge,
    )

    # Objective: the tool actually wrote the item to todos.md.
    assert result.outcome.passed, result.outcome.reason
    assert any("todo_add" in t for t in result.trajectory.run.tool_sequence)
    # Judge: the fuzzy reply-quality pass ran and passed.
    assert result.verdict.judged
    assert result.verdict.passed

    report = aggregate([result])
    assert report.objective_passed == 1
    assert report.judge_passed == 1


async def test_todo_scenario_smoke_objective_fails_when_tool_not_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stubbed_llm: StubbedLLM
) -> None:
    """If the model only narrates and never calls todo_add, the objective
    assertion fails (nothing landed in todos.md) — proving the check is
    real, not a rubber stamp."""
    app = _app(tmp_path, monkeypatch)
    stubbed_llm.load([stub_reply("Sure, I'll remember that.")])

    scen = todo_scenario(item="call the doctor")
    dispatch = build_http_dispatch(
        app, alias="fitt-default", token=PERSONAL_TOKEN, session_id="main"
    )
    result = await run_scenario(
        scen, dispatch=dispatch, snapshot=lambda: snapshot_app(app), judge=None
    )
    assert not result.outcome.passed
    assert not result.verdict.judged  # judge off
