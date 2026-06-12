"""Phase 12 task 8 — the planner pass.

Runs the model with the ``plan``-step prompt and the ``todowrite``
tool offered, so the model *elects* to emit an explicit plan into the
:class:`~gateway.plan_store.PlanStore`. It is one
:func:`~gateway.agent_loop.run_agent_loop` pass, role-switched by the
resolved plan prompt (design D1 — planning is not a separate
subsystem, it's the loop with a different prompt + offered tools).

Elected, not forced (requirements Story 1.1/1.3): the prompt nudges
the model to plan multi-step work; if it judges the request a single
action it simply replies without calling ``todowrite``, and this pass
returns ``planned=False`` with no plan. The executor pass then runs
with whatever plan (or none) exists.

This is the first pass that calls a model. The wiring is unit-tested
with fakes here; whether a *real* model actually plans when nudged is
the eval/dev-loop question (tasks 12f), not a unit test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .agent_loop import AgentLoopResult, run_agent_loop
from .errors import UnknownTool
from .plan_store import Plan
from .prompt_resolver import PromptResolver
from .router import AliasRouter
from .tools import ToolContext, ToolRegistry

_PLAN_TOOL = "todowrite"

# Planning shouldn't loop much: emit a plan (maybe revise once) and
# stop. A tight cap keeps a confused model from burning the budget in
# the planning phase before execution even starts.
_DEFAULT_PLANNER_ITERATIONS = 3


def _executor_tools_hint(tool_registry: ToolRegistry) -> str:
    """Render the executor's toolset as a hint appended to the plan prompt.

    The planner only *calls* ``todowrite``, but it must know what the
    *execution* step can do — otherwise a capable model judges a
    tool-dependent task infeasible and refuses to plan. Validated
    2026-06-11: qwen3:14b went from 2/5 to 10/10 plan-election (clean
    stops, tool-grounded plans) once shown the toolset; a blind planner
    refused with "I don't have internet access". Framed as the execution
    step's tools so the planner plans steps that USE them rather than
    trying to call them itself.

    Returns ``""`` when no tools beyond ``todowrite`` are registered
    (e.g. unit tests), leaving the plain plan prompt unchanged.
    """
    lines: list[str] = []
    for tool in tool_registry.list_all():
        if tool.name == _PLAN_TOOL:
            continue
        desc = " ".join((tool.description or "").split())
        if len(desc) > 100:
            desc = desc[:97] + "..."
        lines.append(f"- {tool.name}: {desc}")
    if not lines:
        return ""
    return (
        "\n\nThe execution step that carries out your plan has these tools "
        "available, so assume each step is achievable with them:\n"
        + "\n".join(lines)
        + "\nPlan concrete steps that use these tools; do not refuse for lack "
        "of access."
    )


@dataclass(slots=True)
class PlannerResult:
    """Outcome of the planner pass.

    * ``plan`` — the plan the model wrote to the store this pass, or
      ``None`` if it elected not to plan.
    * ``planned`` — True iff a non-empty plan was produced.
    * ``loop`` — the underlying :class:`AgentLoopResult` (status,
      tokens, messages) for callers that need the detail.
    """

    plan: Plan | None
    planned: bool
    loop: AgentLoopResult


async def run_planner_pass(
    *,
    alias: str,
    user_message: str,
    alias_router: AliasRouter,
    tool_registry: ToolRegistry,
    approval: Any,
    tool_ctx: ToolContext,
    prompt_resolver: PromptResolver,
    session_key: str,
    max_iterations: int = _DEFAULT_PLANNER_ITERATIONS,
) -> PlannerResult:
    """Run the elected planner pass against ``alias``.

    Offers only the ``todowrite`` tool (the planner decomposes; the
    executor acts), under the ``plan``-step system prompt resolved for
    this alias. Reads the resulting plan from
    ``tool_ctx.plan_store`` after the loop.
    """
    if tool_ctx.plan_store is None:
        raise RuntimeError("planner pass requires a PlanStore wired onto the ToolContext")
    try:
        plan_tool = tool_registry.lookup(_PLAN_TOOL)
    except UnknownTool as exc:
        raise RuntimeError(
            f"planner pass requires the {_PLAN_TOOL!r} tool to be registered"
        ) from exc

    system_prompt = prompt_resolver.resolve("plan", alias) + _executor_tools_hint(tool_registry)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    body_extras: dict[str, Any] = {
        "tools": [plan_tool.to_openai_schema()],
        "tool_choice": "auto",
    }

    result = await run_agent_loop(
        alias=alias,
        messages=messages,
        request_body_extras=body_extras,
        alias_router=alias_router,
        tool_registry=tool_registry,
        approval=approval,
        tool_ctx=tool_ctx,
        session_key=session_key,
        max_iterations=max_iterations,
    )

    plan = tool_ctx.plan_store.get(session_key)
    planned = plan is not None and bool(plan.items)
    return PlannerResult(plan=plan, planned=planned, loop=result)


__all__ = ["PlannerResult", "run_planner_pass"]
