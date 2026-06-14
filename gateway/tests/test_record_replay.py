"""Tests for Phase 12 task 3 — record / replay of model dispatches.

Two layers:

* Unit: key stability, save/load round-trip, keyed + sequential
  replay, and the loud CassetteMiss on an unrecorded request.
* End-to-end: record a full orchestrated turn (planner -> executor)
  through a RecordingRouter, then replay the *same* turn through a
  ReplayRouter with no live router at all — proving the orchestration
  plumbing runs deterministically against recorded real-output shapes.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from gateway.agent_loop import AgentLoopResult
from gateway.config import ModelConfig
from gateway.orchestrator import run_orchestrated_turn
from gateway.plan_store import PlanStore
from gateway.projects import ProjectRegistry
from gateway.prompt_resolver import PromptResolver
from gateway.record_replay import (
    Cassette,
    CassetteMiss,
    RecordingRouter,
    ReplayRouter,
    request_key,
)
from gateway.router import DispatchResult
from gateway.tools import (
    ApprovalBucket,
    ApprovalDecision,
    Tool,
    ToolContext,
    ToolPolicy,
    ToolRegistry,
    ToolResult,
    build_plan_tools,
)

_MODEL = ModelConfig(
    id="m",
    backend="openai",
    endpoint="https://example/v1",
    model="fake/model",
    cost_per_mtok_in=Decimal("0"),
    cost_per_mtok_out=Decimal("0"),
)


def _resp(
    *, tool_calls: list[dict[str, Any]] | None = None, content: str | None = None
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant"}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
        msg["content"] = None
    else:
        msg["content"] = content or ""
    return {
        "id": "r",
        "choices": [
            {
                "index": 0,
                "message": msg,
                "finish_reason": "tool_calls" if tool_calls is not None else "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 10},
    }


class _FakeInner:
    """Minimal real-router stand-in: returns canned responses in order."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)

    async def dispatch(self, alias: str, body: dict[str, Any]) -> DispatchResult:
        resp = self._responses.pop(0)
        return DispatchResult(response=resp, stream=None, model_used=_MODEL, fallback_used=False)

    def resolve(self, alias: str) -> list[ModelConfig]:
        return [_MODEL]


class _AutoApprove:
    async def check(self, tool: Any, args: Any, ctx: Any) -> ApprovalDecision:
        return ApprovalDecision.auto()


async def _noop(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    return ToolResult.ok("ok")


def _registry() -> ToolRegistry:
    reg = ToolRegistry(ToolPolicy.from_config(None))
    for t in build_plan_tools():
        reg.register(t)
    reg.register(
        Tool(
            name="noop",
            description="does nothing",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=_noop,
            default_bucket=ApprovalBucket.AUTO,
        )
    )
    return reg


def _ctx(store: PlanStore) -> ToolContext:
    return ToolContext(
        client="cli",
        session_key="main",
        projects=ProjectRegistry(Path("nonexistent.yaml")),
        plan_store=store,
    )


def _todowrite_call(todos: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "c1",
        "type": "function",
        "function": {"name": "todowrite", "arguments": json.dumps({"todos": todos})},
    }


def _content(result: DispatchResult) -> str:
    assert result.response is not None
    return str(result.response["choices"][0]["message"]["content"])


# --------------------------------------------------------------- unit


def test_request_key_is_stable_and_ignores_volatile_fields() -> None:
    body_a = {"messages": [{"role": "user", "content": "hi"}], "timeout": 300, "stream": False}
    body_b = {"messages": [{"role": "user", "content": "hi"}], "timeout": 5, "stream": True}
    body_c = {"messages": [{"role": "user", "content": "bye"}]}
    assert request_key("a", body_a) == request_key("a", body_b)  # volatile ignored
    assert request_key("a", body_a) != request_key("a", body_c)  # content matters
    assert request_key("a", body_a) != request_key("b", body_a)  # alias matters


async def test_recording_then_replay_round_trip(tmp_path: Path) -> None:
    inner = _FakeInner([_resp(content="hello there")])
    rec = RecordingRouter(inner)
    body = {"messages": [{"role": "user", "content": "hi"}], "timeout": 300}
    await rec.dispatch("fitt-x", body)
    path = tmp_path / "cassette.json"
    rec.save(path)

    replay = ReplayRouter.from_path(path)
    # A different timeout must still hit (volatile field excluded).
    result = await replay.dispatch("fitt-x", {**body, "timeout": 1})
    assert _content(result) == "hello there"
    assert result.model_used.id == "m"


async def test_replay_miss_raises_loudly() -> None:
    replay = ReplayRouter(Cassette())
    with pytest.raises(CassetteMiss):
        await replay.dispatch("fitt-x", {"messages": [{"role": "user", "content": "unrecorded"}]})


async def test_replay_is_sequential_for_duplicate_keys(tmp_path: Path) -> None:
    """The same body recorded twice replays its responses in order."""
    inner = _FakeInner([_resp(content="first"), _resp(content="second")])
    rec = RecordingRouter(inner)
    body = {"messages": [{"role": "user", "content": "same"}]}
    await rec.dispatch("fitt-x", body)
    await rec.dispatch("fitt-x", body)
    path = tmp_path / "c.json"
    rec.save(path)

    replay = ReplayRouter.from_path(path)
    r1 = await replay.dispatch("fitt-x", body)
    r2 = await replay.dispatch("fitt-x", body)
    assert _content(r1) == "first"
    assert _content(r2) == "second"


def test_cassette_load_rejects_wrong_version(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": 999, "interactions": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        Cassette.load(path)


# --------------------------------------------------------------- end-to-end


async def _orchestrate(router: Any, store: PlanStore) -> AgentLoopResult:
    return await run_orchestrated_turn(
        alias="fitt-local-qwen3",
        messages=[{"role": "user", "content": "summarise today's news"}],
        alias_router=router,
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=PromptResolver(),
        session_key="main",
    )


async def test_orchestrated_turn_records_then_replays_identically(tmp_path: Path) -> None:
    """Record a plan -> execute turn through a real-router stand-in,
    then replay the same turn from the cassette with no inner router.
    The replayed turn must reproduce the recorded reply, proving the
    orchestration plumbing runs deterministically off recorded output
    shapes (task 3)."""
    responses = [
        # planner pass (budget 1): emit a 2-step plan
        _resp(tool_calls=[_todowrite_call([{"text": "search news"}, {"text": "summarise"}])]),
        # executor pass: produce the answer
        _resp(content="here is your summary"),
    ]
    rec = RecordingRouter(_FakeInner(responses))
    rec_result = await _orchestrate(rec, PlanStore())
    assert rec_result.assistant_text == "here is your summary"

    path = tmp_path / "turn.json"
    rec.save(path)

    # Replay: brand-new store + replay router, no live inner at all.
    replay = ReplayRouter.from_path(path)
    replay_result = await _orchestrate(replay, PlanStore())
    assert replay_result.assistant_text == "here is your summary"
    assert replay_result.status == "ok"
