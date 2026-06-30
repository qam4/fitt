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

import dataclasses
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .agent_loop import AgentLoopResult, response_to_dict, run_agent_loop
from .errors import NoBackendAvailable, UnknownTool
from .plan_store import Plan
from .prompt_resolver import PromptResolver
from .router import AliasRouter
from .tools import ToolContext, ToolRegistry

_PLAN_TOOL = "todowrite"

# Planning shouldn't loop much: emit a plan (maybe revise once) and
# stop. A tight cap keeps a confused model from burning the budget in
# the planning phase before execution even starts.
_DEFAULT_PLANNER_ITERATIONS = 3

# Planner-level continue-nudge for thinking models (observed-issues
# 2026-06-14, task 14b). A reasoning model (qwen3:14b) plans in its
# `reasoning_content`, returns empty `content` and no `todowrite`, and
# the loop reads that no-tool-call turn as a natural stop — so no plan
# lands. When we detect that *fact* (empty content, no tool call, but
# the model did produce output), we re-prompt once, showing the model
# its own reasoning back and asking it to emit the plan as a tool call.
_PLAN_NUDGE = (
    "You worked out a plan above but did not record it. Call the "
    "`todowrite` tool now to write your plan as an ordered list of "
    "concrete steps. Emit the tool call itself — do not reply in prose."
)

# A planner pass whose dispatch failed (dropped backend / transport
# error) comes back from run_agent_loop as status "upstream_error"
# (agent_loop.py catches the exception rather than re-raising). That's
# transient infra, not a model deciding-not-to-plan, so it's excluded
# from the plan-election denominator (multi-sample convention 3).
_TRANSIENT_LOOP_STATUSES = frozenset({"upstream_error"})


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


def _planner_message(result: AgentLoopResult) -> dict[str, Any] | None:
    """The assistant message dict from the planner pass's final
    response, or ``None`` if it can't be extracted."""
    dumped = response_to_dict(result.response_obj)
    if not dumped:
        return None
    choices = dumped.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    c0 = choices[0]
    msg = c0.get("message") if isinstance(c0, dict) else None
    return msg if isinstance(msg, dict) else None


def _is_thinking_stall(result: AgentLoopResult) -> bool:
    """True when the planner turn produced output but neither a tool
    call nor a usable final reply — the thinking-model failure mode
    (observed-issues 2026-06-14).

    Observable facts only (property C4): the assistant message has no
    ``tool_calls`` and an empty ``content``, yet the model produced
    output (non-empty ``reasoning_content``, or any completion tokens).
    A genuine elect-out has non-empty ``content`` ("no plan needed") and
    is therefore NOT a stall — we must not nudge that case."""
    msg = _planner_message(result)
    if not isinstance(msg, dict):
        return False
    if msg.get("tool_calls"):
        return False
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return False
    reasoning = msg.get("reasoning_content")
    has_reasoning = isinstance(reasoning, str) and bool(reasoning.strip())
    return has_reasoning or result.out_tokens > 0


def _reasoning_text(result: AgentLoopResult) -> str:
    msg = _planner_message(result)
    if not isinstance(msg, dict):
        return ""
    reasoning = msg.get("reasoning_content")
    return reasoning.strip() if isinstance(reasoning, str) else ""


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
    nudge_on_stall: bool = True,
) -> PlannerResult:
    """Run the elected planner pass against ``alias``.

    Offers only the ``todowrite`` tool (the planner decomposes; the
    executor acts), under the ``plan``-step system prompt resolved for
    this alias. Reads the resulting plan from
    ``tool_ctx.plan_store`` after the loop.

    ``nudge_on_stall`` (default on): if the pass produces no plan but
    the model clearly *thought* without acting (the thinking-model
    stall — empty content, no tool call, but output produced), re-prompt
    once, showing the model its own reasoning, to emit the plan as a
    tool call (task 14b).
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

    # Thinking-model continue-nudge (task 14b). If no plan landed but
    # the model thought without acting, re-prompt once — feeding its
    # own reasoning back — to emit the plan via the tool.
    if not planned and nudge_on_stall and _is_thinking_stall(result):
        reasoning = _reasoning_text(result)
        nudge_messages: list[dict[str, Any]] = list(messages)
        if reasoning:
            nudge_messages.append({"role": "assistant", "content": reasoning})
        nudge_messages.append({"role": "user", "content": _PLAN_NUDGE})
        nudge_result = await run_agent_loop(
            alias=alias,
            messages=nudge_messages,
            request_body_extras=body_extras,
            alias_router=alias_router,
            tool_registry=tool_registry,
            approval=approval,
            tool_ctx=tool_ctx,
            session_key=session_key,
            max_iterations=max(2, max_iterations),
        )
        plan = tool_ctx.plan_store.get(session_key)
        planned = plan is not None and bool(plan.items)
        # Fold both passes' usage so cost accounting reflects the nudge.
        result = dataclasses.replace(
            nudge_result,
            in_tokens=result.in_tokens + nudge_result.in_tokens,
            out_tokens=result.out_tokens + nudge_result.out_tokens,
            iterations=result.iterations + nudge_result.iterations,
        )

    return PlannerResult(plan=plan, planned=planned, loop=result)


# --------------------------------------------------------------- plan-election measurement


@dataclass(frozen=True, slots=True)
class PlanElectionSample:
    """One planner-pass attempt for the plan-election measurement.

    ``planned`` — the model emitted a plan (called ``todowrite``).
    ``transient`` — the attempt hit infra trouble (a dropped backend),
    so it's excluded from the denominator (multi-sample convention 3),
    mirroring the scenario runner. Cost fields mirror the suite samples
    (warm latency + tokens) so the grade carries capability AND cost."""

    planned: bool
    transient: bool
    latency_ms: float
    in_tokens: int
    out_tokens: int


@dataclass(frozen=True, slots=True)
class PlanElectionMulti:
    """Aggregate of running the planner pass ``k`` times to measure how
    often an alias *elects* to plan on a multi-step prompt.

    The capability-profile ``plan-election`` dimension (Stage 1 of the
    feature<->capability reconciler): isolates the planner pass (no
    execution) so it's a cheap, direct read of the same election signal
    tasks 23/25 measured on ``daily_news_summary`` (hermes3:8b 0%,
    qwen3:14b ~100%). ``passes`` / ``valid`` mirror the alias-eval
    multi-sample contract; the cost lists feed
    :func:`gateway.capability_profile.grade_from_samples`."""

    alias: str
    samples: list[PlanElectionSample] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.samples)

    @property
    def transient(self) -> int:
        return sum(1 for s in self.samples if s.transient)

    @property
    def valid(self) -> int:
        return self.total - self.transient

    @property
    def passes(self) -> int:
        return sum(1 for s in self.samples if s.planned and not s.transient)

    @property
    def latencies_ms(self) -> list[float]:
        return [s.latency_ms for s in self.samples if not s.transient]

    @property
    def in_tokens(self) -> list[float]:
        return [float(s.in_tokens) for s in self.samples if not s.transient]

    @property
    def out_tokens(self) -> list[float]:
        return [float(s.out_tokens) for s in self.samples if not s.transient]


async def measure_plan_election(
    *,
    alias: str,
    user_message: str,
    samples: int,
    alias_router: AliasRouter,
    tool_registry: ToolRegistry,
    approval: Any,
    make_tool_ctx: Callable[[str], ToolContext],
    prompt_resolver: PromptResolver,
    session_prefix: str = "plan-election",
    nudge_on_stall: bool = True,
    max_iterations: int = _DEFAULT_PLANNER_ITERATIONS,
) -> PlanElectionMulti:
    """Run the planner pass ``samples`` times on a multi-step prompt and
    record how often the alias elects to plan.

    A *fact* for the capability profile — does this model emit a plan
    when handed the planner prompt + executor-tools hint — isolated to
    the planner pass (no execution), so it's far cheaper than a full
    orchestrated scenario. Each sample gets a fresh ``session_key`` (the
    PlanStore is session-keyed, so reuse would leak one sample's plan
    into the next) via ``make_tool_ctx``. A dropped backend is recorded
    transient and excluded from the denominator (convention 3); other
    errors propagate (a real config problem stays fail-loud)."""
    results: list[PlanElectionSample] = []
    for _ in range(samples):
        key = f"{session_prefix}-{uuid.uuid4().hex[:8]}"
        ctx = make_tool_ctx(key)
        start = time.perf_counter()
        try:
            pr = await run_planner_pass(
                alias=alias,
                user_message=user_message,
                alias_router=alias_router,
                tool_registry=tool_registry,
                approval=approval,
                tool_ctx=ctx,
                prompt_resolver=prompt_resolver,
                session_key=key,
                max_iterations=max_iterations,
                nudge_on_stall=nudge_on_stall,
            )
        except NoBackendAvailable:
            results.append(
                PlanElectionSample(
                    planned=False,
                    transient=True,
                    latency_ms=0.0,
                    in_tokens=0,
                    out_tokens=0,
                )
            )
            continue
        latency_ms = (time.perf_counter() - start) * 1000.0
        transient = pr.loop.status in _TRANSIENT_LOOP_STATUSES
        results.append(
            PlanElectionSample(
                planned=pr.planned and not transient,
                transient=transient,
                latency_ms=latency_ms,
                in_tokens=pr.loop.in_tokens,
                out_tokens=pr.loop.out_tokens,
            )
        )
    return PlanElectionMulti(alias=alias, samples=results)


__all__ = [
    "PlanElectionMulti",
    "PlanElectionSample",
    "PlannerResult",
    "measure_plan_election",
    "run_planner_pass",
]
