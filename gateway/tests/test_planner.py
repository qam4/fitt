"""Tests for Phase 12 task 8 — the planner pass.

Wiring tested with fakes (no real model): a stub router returns a
``todowrite`` tool_call, the loop executes it (auto-approved), and the
plan lands in the PlanStore. Also the elected-skip path (model
replies without planning) and that the plan-step prompt is what's
sent.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from gateway.config import ModelConfig
from gateway.plan_store import PlanStore
from gateway.planner import run_planner_pass
from gateway.projects import ProjectRegistry
from gateway.prompt_resolver import PromptResolver
from gateway.router import DispatchResult
from gateway.tools import (
    ApprovalDecision,
    ToolContext,
    ToolPolicy,
    ToolRegistry,
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
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    content: str | None = None,
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


class _SeqRouter:
    """Returns canned responses in order; captures dispatched bodies."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.bodies: list[dict[str, Any]] = []

    async def dispatch(self, alias: str, body: dict[str, Any]) -> DispatchResult:
        self.bodies.append(body)
        resp = self._responses.pop(0)
        return DispatchResult(response=resp, stream=None, model_used=_MODEL, fallback_used=False)

    def resolve(self, alias: str) -> list[ModelConfig]:
        return [_MODEL]


class _AutoApprove:
    async def check(self, tool: Any, args: Any, ctx: Any) -> ApprovalDecision:
        return ApprovalDecision.auto()


def _registry() -> ToolRegistry:
    reg = ToolRegistry(ToolPolicy.from_config(None))
    for t in build_plan_tools():
        reg.register(t)
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


async def test_planner_writes_plan_when_model_elects() -> None:
    store = PlanStore()
    router = _SeqRouter(
        [
            _resp(tool_calls=[_todowrite_call([{"text": "search news"}, {"text": "summarise"}])]),
            _resp(content="plan ready"),
        ]
    )
    result = await run_planner_pass(
        alias="fitt-local-qwen3",
        user_message="summarise today's news",
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=PromptResolver(),
        session_key="main",
    )
    assert result.planned is True
    assert result.plan is not None
    assert [i.text for i in result.plan.items] == ["search news", "summarise"]
    # Persisted to the store under the session.
    assert store.get("main") == result.plan


async def test_planner_elects_not_to_plan() -> None:
    store = PlanStore()
    router = _SeqRouter([_resp(content="that's a single step; no plan needed")])
    result = await run_planner_pass(
        alias="fitt-local-qwen3",
        user_message="what time is it",
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=PromptResolver(),
        session_key="main",
    )
    assert result.planned is False
    assert result.plan is None
    assert store.get("main") is None


async def test_planner_sends_plan_step_prompt_and_offers_todowrite() -> None:
    store = PlanStore()
    router = _SeqRouter([_resp(content="ok")])
    resolver = PromptResolver()
    await run_planner_pass(
        alias="fitt-local-qwen3",
        user_message="do a thing",
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=resolver,
        session_key="main",
    )
    body = router.bodies[0]
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"] == resolver.resolve("plan", "fitt-local-qwen3")
    assert body["messages"][1] == {"role": "user", "content": "do a thing"}
    offered = [t["function"]["name"] for t in body["tools"]]
    assert offered == ["todowrite"]
