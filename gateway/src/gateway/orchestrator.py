"""Phase 12 task 10 — the orchestrator.

Runs one turn as **plan -> execute** and is shaped as a drop-in for
:func:`gateway.agent_loop.run_agent_loop`: same call surface (alias,
messages, request_body_extras, collaborators) and the same
:class:`AgentLoopResult` return, so the chat handler and cron runner
can route a turn through it without changing any downstream handling
(memory append, envelope shaping, detached delivery, cost accounting).

It is a state machine, not an agent (design D1) — the intelligence
lives in the two passes; the orchestrator owns the sequence:

1. **Plan pass** (lean, ``todowrite``-only) via
   :func:`gateway.planner.run_planner_pass` on the latest user turn.
   Elected: the model decides whether to emit a plan.
2. **Execute pass** via ``run_agent_loop`` on the *full assembled
   messages* (identity + capability block preserved), with the
   execute-step prompt and the plan re-injected into the system
   message (property C1), offering the original toolset.

The returned ``AgentLoopResult`` is the execute pass's, with the
planner pass's token and iteration counts folded in so cost
accounting reflects both passes.

Recovery (tasks 13/14) and turn-event emission (task 17) are
deliberate later additions; this is the bare plan -> execute spine.
Whether a turn is routed here at all is gated per alias by
``Config.is_orchestrated`` (default off) — see the chat/cron wiring.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from .agent_loop import AgentLoopResult, run_agent_loop
from .plan_store import Plan
from .planner import run_planner_pass
from .prompt_resolver import PromptResolver
from .router import AliasRouter
from .tools import ToolContext, ToolRegistry

# Per-pass iteration caps. Planning is tight (emit a plan, maybe
# revise once); execution gets the larger budget. Task 11 replaces
# these literals with per-alias config; until then they mirror the
# primitives' own defaults so behaviour is unchanged.
_DEFAULT_PLANNER_ITERATIONS = 3
_DEFAULT_EXECUTOR_ITERATIONS = 10


def _latest_user_message(messages: list[dict[str, Any]]) -> str:
    """The most recent user-role text in the assembled messages.

    The plan pass is deliberately lean — it plans against the user's
    actual request, not the whole transcript (matching the focused
    planner prompt). Returns ``""`` if there's no user message (the
    planner then simply elects not to plan)."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
    return ""


def _plan_block(plan: Plan) -> str:
    return (
        "[Plan]\n"
        + plan.render_markdown()
        + "\n\nWork this plan: do the next step that is not yet done, and call "
        "`todowrite` to mark each step done as you complete it. When every step "
        "is done, give your final answer."
    )


def _augment_system(messages: list[dict[str, Any]], additions: list[str]) -> list[dict[str, Any]]:
    """Return a copy of ``messages`` with ``additions`` merged into the
    system message (appended to an existing leading system message, or
    prepended as one if absent). Preserves the assembled identity /
    capability prompt — we add to it, never replace it."""
    extra = "\n\n".join(a for a in additions if a and a.strip())
    if not extra:
        return list(messages)
    out = [dict(m) for m in messages]
    if out and out[0].get("role") == "system" and isinstance(out[0].get("content"), str):
        out[0] = {**out[0], "content": out[0]["content"] + "\n\n" + extra}
    else:
        out.insert(0, {"role": "system", "content": extra})
    return out


async def run_orchestrated_turn(
    *,
    alias: str,
    messages: list[dict[str, Any]],
    request_body_extras: dict[str, Any] | None = None,
    alias_router: AliasRouter,
    tool_registry: ToolRegistry,
    approval: Any,
    tool_ctx: ToolContext,
    prompt_resolver: PromptResolver,
    session_key: str,
    planner_max_iterations: int = _DEFAULT_PLANNER_ITERATIONS,
    executor_max_iterations: int = _DEFAULT_EXECUTOR_ITERATIONS,
    artifact_store: Any = None,
) -> AgentLoopResult:
    """Run one turn as plan -> execute and return the execute pass's
    :class:`AgentLoopResult` (with the planner pass's tokens folded in).

    The planner pass runs every turn — election is the model's call
    inside it, not a branch here — so there is no separate fast path
    (a deliberate design choice; latency optimisation is a non-goal).
    """
    planner = await run_planner_pass(
        alias=alias,
        user_message=_latest_user_message(messages),
        alias_router=alias_router,
        tool_registry=tool_registry,
        approval=approval,
        tool_ctx=tool_ctx,
        prompt_resolver=prompt_resolver,
        session_key=session_key,
        max_iterations=planner_max_iterations,
    )

    plan: Plan | None = None
    if tool_ctx.plan_store is not None:
        plan = tool_ctx.plan_store.get(session_key)

    additions: list[str] = []
    execute_prompt = prompt_resolver.resolve("execute", alias)
    if execute_prompt.strip():
        additions.append(execute_prompt.strip())
    if plan is not None and plan.items:
        additions.append(_plan_block(plan))

    exec_messages = _augment_system(messages, additions)
    exec_result = await run_agent_loop(
        alias=alias,
        messages=exec_messages,
        request_body_extras=request_body_extras,
        alias_router=alias_router,
        tool_registry=tool_registry,
        approval=approval,
        tool_ctx=tool_ctx,
        session_key=session_key,
        max_iterations=executor_max_iterations,
        artifact_store=artifact_store,
    )

    # Fold the planner pass's usage into the returned result so cost
    # accounting upstream sees both passes.
    return dataclasses.replace(
        exec_result,
        in_tokens=exec_result.in_tokens + planner.loop.in_tokens,
        out_tokens=exec_result.out_tokens + planner.loop.out_tokens,
        iterations=exec_result.iterations + planner.loop.iterations,
    )


__all__ = ["run_orchestrated_turn"]
