"""Tests for :mod:`gateway.scenario_eval` — the headless scenario
runner (Phase 12 tasks 4 + 22).

The live-model behaviour is exercised against EC2 by hand; here we
pin the pure plumbing with fakes:

* aggregation math (the task-22 pass-rate: completed / valid, with
  transient infra failures excluded);
* the AgentLoopResult -> ScenarioSampleResult mapping;
* the dispatch wiring — flat vs planned, tool injection, and a fresh
  session_key per sample — by monkeypatching the loop entry points so
  no real router/model is needed.
"""

from __future__ import annotations

from typing import Any

import pytest

from gateway.agent_loop import AgentLoopResult
from gateway.memory import PersistedToolCall
from gateway.scenario_eval import (
    ScenarioMultiResult,
    ScenarioSampleResult,
    run_scenario_multi,
    run_scenario_once,
)
from gateway.scenarios import daily_news_summary

# --------------------------------------------------------------- fakes


class _FakeTool:
    def __init__(self, name: str) -> None:
        self._name = name

    def to_openai_schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self._name}}


class _FakeRegistry:
    def __init__(self, names: list[str]) -> None:
        self._tools = [_FakeTool(n) for n in names]

    def list_all(self) -> list[_FakeTool]:
        return list(self._tools)


def _call(tool_name: str, *, ok: bool = True) -> PersistedToolCall:
    return PersistedToolCall(
        tool_name=tool_name,
        args={},
        result_status="ok" if ok else "error",
        result_summary="",
    )


def _loop_result(
    *,
    status: str = "ok",
    assistant_text: str = "",
    calls: list[PersistedToolCall] | None = None,
) -> AgentLoopResult:
    return AgentLoopResult(
        status=status,
        assistant_text=assistant_text,
        iterations=2,
        in_tokens=100,
        out_tokens=50,
        tool_calls_for_memory=calls or [],
    )


def _sample(outcome: str) -> ScenarioSampleResult:
    return ScenarioSampleResult(
        mode="flat",
        outcome=outcome,  # type: ignore[arg-type]
        loop_status="ok",
        iterations=1,
        in_tokens=0,
        out_tokens=0,
        tool_sequence=(),
        assistant_preview="",
    )


# --------------------------------------------------------------- aggregation


def test_multi_result_aggregation() -> None:
    res = ScenarioMultiResult(
        scenario_name="daily_news_summary",
        mode="flat",
        alias="fitt-ec2-hermes",
        samples=[
            _sample("completed"),
            _sample("completed"),
            _sample("no_search"),
            _sample("searched_not_delivered"),
        ],
    )
    assert res.total == 4
    assert res.passes == 2
    assert res.transient == 0
    assert res.valid == 4
    assert res.pass_rate == 0.5
    assert res.outcome_counts == {
        "completed": 2,
        "no_search": 1,
        "searched_not_delivered": 1,
    }


def test_multi_result_excludes_transient_from_denominator() -> None:
    res = ScenarioMultiResult(
        scenario_name="s",
        mode="flat",
        alias="a",
        samples=[_sample("completed"), _sample("upstream_error"), _sample("completed")],
    )
    assert res.transient == 1
    assert res.valid == 2
    assert res.pass_rate == 1.0  # 2/2; the transient sample doesn't drag it


def test_multi_result_all_transient_rate_is_none() -> None:
    res = ScenarioMultiResult(
        scenario_name="s",
        mode="flat",
        alias="a",
        samples=[_sample("upstream_error"), _sample("upstream_error")],
    )
    assert res.valid == 0
    assert res.pass_rate is None


# --------------------------------------------------------------- dispatch wiring


async def test_flat_run_injects_tools_and_classifies(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    async def fake_loop(**kwargs: Any) -> AgentLoopResult:
        captured.update(kwargs)
        return _loop_result(
            assistant_text="x" * 300,
            calls=[_call("web_search")],
        )

    monkeypatch.setattr("gateway.agent_loop.run_agent_loop", fake_loop)

    sample = await run_scenario_once(
        daily_news_summary(),
        "fitt-ec2-hermes",
        "flat",
        alias_router=object(),
        tool_registry=_FakeRegistry(["web_search", "send_message"]),
        approval=object(),
        make_tool_ctx=lambda key: {"session_key": key},
        system_prompt="[Capabilities] ...",
    )

    # Classified as completed (searched + substantive reply).
    assert sample.outcome == "completed"
    assert sample.mode == "flat"
    assert sample.tool_sequence == ("web_search:ok",)
    # Tools were injected from the registry into the request body.
    extras = captured["request_body_extras"]
    names = [t["function"]["name"] for t in extras["tools"]]
    assert names == ["web_search", "send_message"]
    assert extras["tool_choice"] == "auto"
    # System prompt rode along as a leading system message.
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][1]["role"] == "user"


async def test_planned_run_requires_prompt_resolver(monkeypatch: Any) -> None:
    with pytest.raises(ValueError, match="prompt_resolver"):
        await run_scenario_once(
            daily_news_summary(),
            "fitt-ec2-hermes",
            "planned",
            alias_router=object(),
            tool_registry=_FakeRegistry(["web_search"]),
            approval=object(),
            make_tool_ctx=lambda key: {"session_key": key},
        )


async def test_multi_uses_unique_session_keys(monkeypatch: Any) -> None:
    keys: list[str] = []

    async def fake_loop(**kwargs: Any) -> AgentLoopResult:
        keys.append(kwargs["session_key"])
        return _loop_result(calls=[_call("web_search")], assistant_text="x" * 300)

    monkeypatch.setattr("gateway.agent_loop.run_agent_loop", fake_loop)

    res = await run_scenario_multi(
        daily_news_summary(),
        "fitt-ec2-hermes",
        "flat",
        samples=4,
        alias_router=object(),
        tool_registry=_FakeRegistry(["web_search"]),
        approval=object(),
        make_tool_ctx=lambda key: {"session_key": key},
    )
    assert res.total == 4
    assert res.passes == 4
    # Each sample got its own session_key (PlanStore independence).
    assert len(set(keys)) == 4


async def test_dropped_backend_becomes_transient_not_a_crash(monkeypatch: Any) -> None:
    """A NoBackendAvailable mid-run (e.g. the EC2 tunnel blipping) is
    recorded as a transient `upstream_error` sample and the sweep
    continues — one blip must not nuke the whole multi-sample run."""
    from gateway.errors import NoBackendAvailable

    calls = {"n": 0}

    async def flaky_loop(**kwargs: Any) -> AgentLoopResult:
        calls["n"] += 1
        # Second sample's dispatch drops; the others succeed.
        if calls["n"] == 2:
            raise NoBackendAvailable("fitt-ec2-hermes", ["hermes3-8b-ec2"])
        return _loop_result(calls=[_call("web_search")], assistant_text="x" * 300)

    monkeypatch.setattr("gateway.agent_loop.run_agent_loop", flaky_loop)

    res = await run_scenario_multi(
        daily_news_summary(),
        "fitt-ec2-hermes",
        "flat",
        samples=3,
        alias_router=object(),
        tool_registry=_FakeRegistry(["web_search"]),
        approval=object(),
        make_tool_ctx=lambda key: {"session_key": key},
    )
    # All 3 samples ran (no crash); one is transient.
    assert res.total == 3
    assert res.transient == 1
    assert res.passes == 2
    # Transient excluded from the denominator: 2/2 = 1.0.
    assert res.valid == 2
    assert res.pass_rate == 1.0
    assert res.outcome_counts["upstream_error"] == 1
