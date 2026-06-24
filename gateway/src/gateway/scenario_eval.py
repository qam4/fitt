"""Headless scenario runner (Phase 12 tasks 4 + 22).

Runs a whole multi-step :class:`~gateway.scenarios.Scenario` through
either the flat agent loop or the plan -> execute orchestrator, against
a real model, and classifies the structural outcome via
:func:`gateway.scenarios.classify_news_outcome`. Multi-samples for a
capability pass-rate (the Phase 12 task-2 conventions).

This is the engine two tasks share:

* **Task 4** runs the *flat* loop on ``daily_news_summary`` to read the
  baseline failure.
* **Task 22** runs the same scenario through *both* modes and compares
  pass rates ("did planning beat the flat-loop read").

It owns no wiring. The caller (the ``fitt scenario`` CLI) builds the
real :class:`~gateway.tools.ToolRegistry`, :class:`~gateway.router.AliasRouter`,
approval wrapper, and a per-sample :class:`~gateway.tools.ToolContext`
factory — the same objects the gateway builds — and passes them in.
Keeping the wiring out here means the runner exercises the *real* tool
path (same registry, same approval policy, same prompts), not a
re-wired copy, and stays unit-testable with fakes.

Why a fresh session per sample
------------------------------

Each sample generates a unique ``session_key`` so samples are
independent: the :class:`~gateway.plan_store.PlanStore` is keyed by
session, so reusing a key would leak one sample's plan into the next.
The runner generates the key and hands it to both the loop and the
``make_tool_ctx`` factory so the ToolContext and the dispatch agree.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from .scenarios import (
    TRANSIENT_OUTCOMES,
    Scenario,
    ScenarioOutcome,
    classify_news_outcome,
)

if TYPE_CHECKING:
    from .agent_loop import AgentLoopResult
    from .prompt_resolver import PromptResolver
    from .router import AliasRouter
    from .tools import ToolContext, ToolRegistry


ScenarioMode = Literal["flat", "planned"]

# Preview length for the assistant reply we keep on a sample, so an
# operator reading the report sees what the model actually said
# without the full (possibly multi-paragraph) text.
_PREVIEW_CHARS = 200


@dataclass(frozen=True, slots=True)
class ScenarioSampleResult:
    """One run of a scenario, classified structurally.

    ``tool_sequence`` is the ordered ``"<tool>:<result_status>"`` list
    of every executed call — the fact that tells the operator *what the
    loop actually did* (searched then sent, narrated with no call,
    looped on a failing tool), which is the whole point of the task-4
    read.

    ``plan_produced`` (planned mode only): whether the planner emitted
    a plan (called ``todowrite``). Answers task 23 directly: does the
    alias *elect* to plan when the orchestrator gives it the chance?"""

    mode: ScenarioMode
    outcome: ScenarioOutcome
    loop_status: str
    iterations: int
    in_tokens: int
    out_tokens: int
    tool_sequence: tuple[str, ...]
    assistant_preview: str
    plan_produced: bool | None = None


@dataclass(frozen=True, slots=True)
class ScenarioMultiResult:
    """Aggregate of running one scenario in one mode ``k`` times.

    ``pass_rate`` counts ``completed`` over *valid* samples (transient
    infra failures excluded, mirroring the alias-eval multi-sample
    contract). ``None`` when every sample was transient (no signal)."""

    scenario_name: str
    mode: ScenarioMode
    alias: str
    samples: list[ScenarioSampleResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.samples)

    @property
    def passes(self) -> int:
        return sum(1 for s in self.samples if s.outcome == "completed")

    @property
    def transient(self) -> int:
        return sum(1 for s in self.samples if s.outcome in TRANSIENT_OUTCOMES)

    @property
    def valid(self) -> int:
        return self.total - self.transient

    @property
    def pass_rate(self) -> float | None:
        return self.passes / self.valid if self.valid else None

    @property
    def outcome_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for s in self.samples:
            counts[s.outcome] = counts.get(s.outcome, 0) + 1
        return counts

    @property
    def plan_election_rate(self) -> float | None:
        """Fraction of planned-mode samples where the planner actually
        produced a plan. ``None`` for flat mode or when no samples
        report plan_produced (pre-task-23 data)."""
        planned = [s for s in self.samples if s.plan_produced is not None]
        if not planned:
            return None
        return sum(1 for s in planned if s.plan_produced) / len(planned)


def _tool_sequence(result: AgentLoopResult) -> tuple[str, ...]:
    return tuple(f"{c.tool_name}:{c.result_status}" for c in result.tool_calls_for_memory)


def _summarize(
    result: AgentLoopResult,
    mode: ScenarioMode,
    scenario: Scenario,
    *,
    plan_produced: bool | None = None,
    preview_chars: int = _PREVIEW_CHARS,
) -> ScenarioSampleResult:
    return ScenarioSampleResult(
        mode=mode,
        outcome=classify_news_outcome(result, scenario),
        loop_status=result.status,
        iterations=result.iterations,
        in_tokens=result.in_tokens,
        out_tokens=result.out_tokens,
        tool_sequence=_tool_sequence(result),
        assistant_preview=result.assistant_text.strip()[:preview_chars],
        plan_produced=plan_produced,
    )


async def run_scenario_once(
    scenario: Scenario,
    alias: str,
    mode: ScenarioMode,
    *,
    alias_router: AliasRouter,
    tool_registry: ToolRegistry,
    approval: Any,
    make_tool_ctx: Callable[[str], ToolContext],
    system_prompt: str = "",
    prompt_resolver: PromptResolver | None = None,
    planner_alias: str = "",
    planner_max_iterations: int | None = None,
    executor_max_iterations: int | None = None,
    flat_max_iterations: int | None = None,
    session_key: str | None = None,
    preview_chars: int = _PREVIEW_CHARS,
) -> ScenarioSampleResult:
    """Run ``scenario`` once in ``mode`` and classify the outcome.

    ``flat`` dispatches :func:`~gateway.agent_loop.run_agent_loop`
    directly (the current loop, no planning). ``planned`` dispatches
    :func:`~gateway.orchestrator.run_orchestrated_turn` (plan ->
    execute) and requires ``prompt_resolver``.

    FITT's registered tools are injected into the request body (same
    shape the HTTP endpoint and cron runner use) unless the caller
    already supplied a ``tools`` key via the request extras — they
    don't here, so we always inject from ``tool_registry``."""
    from .agent_loop import run_agent_loop
    from .errors import NoBackendAvailable

    key = session_key or f"scenario-{scenario.name}-{uuid.uuid4().hex[:8]}"

    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": scenario.user_message})

    extras: dict[str, Any] = {
        "tools": [t.to_openai_schema() for t in tool_registry.list_all()],
        "tool_choice": "auto",
    }

    tool_ctx = make_tool_ctx(key)

    try:
        if mode == "planned":
            if prompt_resolver is None:
                raise ValueError("planned mode requires a prompt_resolver")
            from .orchestrator import run_orchestrated_turn

            result = await run_orchestrated_turn(
                alias=alias,
                messages=messages,
                request_body_extras=extras,
                alias_router=alias_router,
                tool_registry=tool_registry,
                approval=approval,
                tool_ctx=tool_ctx,
                prompt_resolver=prompt_resolver,
                session_key=key,
                planner_alias=planner_alias,
                planner_max_iterations=planner_max_iterations,
                executor_max_iterations=executor_max_iterations,
            )
            # Task 23: check whether the planner actually produced a plan.
            plan_store = getattr(tool_ctx, "plan_store", None)
            plan_produced: bool | None = None
            if plan_store is not None:
                plan = plan_store.get(key)
                plan_produced = plan is not None and bool(plan.items)
        else:
            plan_produced = None
            loop_kwargs: dict[str, Any] = {}
            if flat_max_iterations is not None:
                loop_kwargs["max_iterations"] = flat_max_iterations
            result = await run_agent_loop(
                alias=alias,
                messages=messages,
                request_body_extras=extras,
                alias_router=alias_router,
                tool_registry=tool_registry,
                approval=approval,
                tool_ctx=tool_ctx,
                session_key=key,
                **loop_kwargs,
            )
    except NoBackendAvailable as exc:
        # A dropped backend mid-run (e.g. the EC2-over-SSM tunnel
        # blipping) is transient infra, not a capability miss.
        # run_agent_loop re-raises NoBackendAvailable as a hard error
        # (right for the chat path, which maps it to HTTP 502); here we
        # record it as an `upstream_error` sample so multi-sampling
        # excludes it from the denominator (convention 3) and the
        # remaining samples still run, instead of one blip nuking the
        # whole sweep. UnknownAlias is NOT caught — that's a real config
        # error and stays fail-loud (P11).
        return ScenarioSampleResult(
            mode=mode,
            outcome="upstream_error",
            loop_status="upstream_error",
            iterations=0,
            in_tokens=0,
            out_tokens=0,
            tool_sequence=(),
            assistant_preview=f"NoBackendAvailable: {exc}"[:preview_chars],
            plan_produced=None,
        )

    return _summarize(
        result, mode, scenario, plan_produced=plan_produced, preview_chars=preview_chars
    )


async def run_scenario_multi(
    scenario: Scenario,
    alias: str,
    mode: ScenarioMode,
    *,
    samples: int = 5,
    **kwargs: Any,
) -> ScenarioMultiResult:
    """Run ``scenario`` ``samples`` times in ``mode`` and aggregate.

    Sequential (one shared backend quota, same posture as the
    alias-eval multi-sample runner). Each sample gets a fresh
    ``session_key`` for independence."""
    results: list[ScenarioSampleResult] = []
    for _ in range(samples):
        results.append(await run_scenario_once(scenario, alias, mode, **kwargs))
    return ScenarioMultiResult(
        scenario_name=scenario.name,
        mode=mode,
        alias=alias,
        samples=results,
    )
