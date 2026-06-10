"""Phase 12 task 9 — the executor pass.

Runs the model with the ``execute``-step prompt and the current plan
**re-injected** from the :class:`~gateway.plan_store.PlanStore`, so the
model works the plan it (or a prior turn) produced and ticks todos as
it goes. Like the planner pass it is one
:func:`~gateway.agent_loop.run_agent_loop` pass, role-switched by the
resolved prompt (design D1) — the difference is *which* prompt and
*which* tools are offered: the executor gets the full registered
toolset (it has to actually do the work), not just ``todowrite``.

Correctness property C1: once a plan exists in the store, every
execute pass re-injects it — a produced plan is never silently
ignored. This module enforces that by reading the plan from the store
and rendering it into the system message whenever one is present.

The executor does not *require* a plan. When the planner elected not
to plan (single-action turn), there's nothing to re-inject and the
pass runs as an ordinary tool-use loop under the execute prompt — C1
is vacuously satisfied.

The identity / capability-block system prefix (the one the chat
handler normally assembles) is the orchestrator's responsibility
(task 10); this primitive focuses on the execute prompt + plan
re-injection, mirroring how :func:`run_planner_pass` focuses on the
plan prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .agent_loop import AgentLoopResult, run_agent_loop
from .plan_store import Plan
from .prompt_resolver import PromptResolver
from .router import AliasRouter
from .tools import ToolContext, ToolRegistry

# Execution may legitimately take several tool-use iterations
# (read a few files, grep, run a command, summarise). Default to
# the agent loop's own cap; task 11 makes this configurable per
# alias (higher for planned turns) — until then we don't hardcode
# a second magic number, we reuse the loop's default by leaving
# it overridable here.
_DEFAULT_EXECUTOR_ITERATIONS = 10


@dataclass(slots=True)
class ExecutorResult:
    """Outcome of the executor pass.

    * ``plan`` — the plan as it stands in the store *after* the pass
      (the executor may have ticked items via ``todowrite``), or
      ``None`` if there was never a plan.
    * ``plan_complete`` — True iff a plan exists and every item is
      ``done``.
    * ``loop`` — the underlying :class:`AgentLoopResult`.
    """

    plan: Plan | None
    plan_complete: bool
    loop: AgentLoopResult


def _build_execute_system(execute_prompt: str, plan: Plan | None) -> str | None:
    """Compose the executor's system message from the resolved
    execute prompt and the re-injected plan.

    Returns ``None`` when there's nothing to say (empty execute
    prompt and no plan), so the caller omits the system message
    entirely rather than sending an empty one.
    """
    parts: list[str] = []
    if execute_prompt.strip():
        parts.append(execute_prompt.strip())
    if plan is not None and plan.items:
        parts.append(
            "[Plan]\n"
            + plan.render_markdown()
            + "\n\nWork this plan: do the next step that is not yet done, "
            "and call `todowrite` to mark each step done as you complete "
            "it. When every step is done, give your final answer."
        )
    if not parts:
        return None
    return "\n\n".join(parts)


async def run_executor_pass(
    *,
    alias: str,
    user_message: str,
    alias_router: AliasRouter,
    tool_registry: ToolRegistry,
    approval: Any,
    tool_ctx: ToolContext,
    prompt_resolver: PromptResolver,
    session_key: str,
    max_iterations: int = _DEFAULT_EXECUTOR_ITERATIONS,
) -> ExecutorResult:
    """Run the executor pass against ``alias``.

    Offers the full registered toolset under the ``execute``-step
    system prompt, with the current plan (if any) re-injected from
    ``tool_ctx.plan_store``. Reads the plan back after the loop so
    callers see any todo ticks the executor made.
    """
    plan_before: Plan | None = None
    if tool_ctx.plan_store is not None:
        plan_before = tool_ctx.plan_store.get(session_key)

    execute_prompt = prompt_resolver.resolve("execute", alias)
    system_content = _build_execute_system(execute_prompt, plan_before)

    messages: list[dict[str, Any]] = []
    if system_content is not None:
        messages.append({"role": "system", "content": system_content})
    messages.append({"role": "user", "content": user_message})

    body_extras: dict[str, Any] = {
        "tools": [t.to_openai_schema() for t in tool_registry.list_all()],
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

    plan_after: Plan | None = None
    if tool_ctx.plan_store is not None:
        plan_after = tool_ctx.plan_store.get(session_key)
    plan_complete = plan_after is not None and plan_after.is_complete()

    return ExecutorResult(plan=plan_after, plan_complete=plan_complete, loop=result)


__all__ = ["ExecutorResult", "run_executor_pass"]
