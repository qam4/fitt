"""Phase 12.5a — the live capability-profile producer.

The pure profile data model + grading + diff live in
:mod:`gateway.capability_profile` (no live model). This module is
the **live producer**: given a wired gateway ``state``, it runs
the eval suites + plan-election, reads declared metadata, and
builds a :class:`~gateway.capability_profile.CapabilityProfile`.

Shared by BOTH entry points so they cannot drift (Req 1.5,
Property 5):

* ``fitt profile alias`` (CLI) — builds ``state`` via ``create_app``.
* ``POST /v1/profile/<alias>`` — uses the running app's ``state``.

The producer returns the profile object only; the caller writes
+ diffs + renders it (the CLI prints, the endpoint returns JSON).
Keeping the wiring identical to the scenario runner / CLI means
this exercises the real tool path, not a re-wired copy.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .capability_profile import (
    CapabilityProfile,
    declared_from_ollama_tags,
    grade_from_samples,
)

if TYPE_CHECKING:
    from .config import Config

_log = logging.getLogger(__name__)


def suite_grade(name: str, results: list[Any]) -> Any:
    """Fold a multi-sample eval suite's per-case results into one
    :class:`~gateway.capability_profile.MeasuredGrade` (pass-rate
    over valid samples + warm latency). Lifted verbatim from the
    CLI producer so the grade math is shared."""
    passes = sum(r.passes for r in results)
    valid = sum(r.valid for r in results)
    total = sum(r.total for r in results)
    latencies = [s.latency_ms for r in results for s in r.samples]
    return grade_from_samples(
        name,
        passes=passes,
        valid=valid,
        samples=total,
        latencies_ms=latencies,
    )


async def run_profile(
    *,
    alias: str,
    cfg: Config,
    state: Any,
    samples: int = 5,
    timeout_s: float = 30.0,
) -> CapabilityProfile:
    """Produce a capability profile for ``alias`` against the live
    backend, using the wiring already on the gateway ``state``
    (tool registry, approval, plan store, prompt resolver, ...).

    Declared facts come free from Ollama ``/api/tags`` (other
    backends carry no declared block). Measured grades:
    ``tool-calling`` (realistic suite), ``coding``, and
    ``plan-election`` (the planner pass, k samples). Returns the
    profile; the caller persists / diffs / renders it.
    """
    import httpx

    from .alias_eval import realistic_cases, run_eval_suite_multi
    from .alias_eval_coding import default_coding_cases
    from .capabilities import build_capability_block
    from .cron_runner import _AutoApproveWrapper
    from .planner import measure_plan_election
    from .router import AliasRouter
    from .scenarios import daily_news_summary
    from .tools import ToolContext

    registry = state.tool_registry
    router = AliasRouter(cfg)
    primary = router.resolve(alias)[0]
    # Auto-approve so the planner pass's todowrite doesn't block on
    # an approval tap that won't come in this headless run; the deny
    # list is still enforced.
    approval = _AutoApproveWrapper(state.approval)
    system_prompt = build_capability_block(registry) if registry.list_names() else ""

    def _make_ctx(session_key: str) -> ToolContext:
        return ToolContext(
            client="cli",
            session_key=session_key,
            projects=state.project_registry,
            backend=state.execution_backend,
            policy=registry.policy,
            audit=state.audit,
            cron=state.cron,
            events=state.events,
            local_shell=state.local_shell,
            lessons=state.lessons,
            turns=None,
            turn_id=None,
            web_search_backend=cfg.web.search_backend,
            plan_store=state.plan_store,
        )

    # Declared facts — Ollama only; a non-Ollama backend (or an
    # unreachable one) just carries no declared block, the profile
    # still has its measured grades.
    declared: list[Any] = []
    resource: Any = None
    if primary.backend == "ollama":
        try:
            resp = httpx.get(f"{primary.endpoint}/api/tags", timeout=10.0)
            resp.raise_for_status()
            declared, resource = declared_from_ollama_tags(resp.json(), primary.model)
        except Exception as exc:
            _log.warning(
                "profile.declared_failed",
                extra={"alias": alias, "error": f"{type(exc).__name__}: {exc}"},
            )

    tool_results = await run_eval_suite_multi(
        alias,
        router,
        cases=realistic_cases(),
        samples=samples,
        timeout_s=timeout_s,
        system_prompt=system_prompt,
    )
    # Coding cases embed their own realistic system prompt in the
    # prompt text, so no separate system_prompt here.
    coding_results = await run_eval_suite_multi(
        alias,
        router,
        cases=default_coding_cases(),
        samples=samples,
        timeout_s=timeout_s,
    )
    # Plan-election: run the planner pass on the canonical multi-step
    # prompt and record how often the alias emits a plan (Stage 1).
    election = await measure_plan_election(
        alias=alias,
        user_message=daily_news_summary().user_message,
        samples=samples,
        alias_router=router,
        tool_registry=registry,
        approval=approval,
        make_tool_ctx=_make_ctx,
        prompt_resolver=state.prompt_resolver,
    )

    return CapabilityProfile(
        alias=alias,
        model_id=primary.id,
        captured_at=datetime.now(UTC),
        declared=declared,
        measured=[
            suite_grade("tool-calling", tool_results),
            suite_grade("coding", coding_results),
            grade_from_samples(
                "plan-election",
                passes=election.passes,
                valid=election.valid,
                samples=election.total,
                latencies_ms=election.latencies_ms,
                in_tokens=election.in_tokens,
                out_tokens=election.out_tokens,
                notes="share of planner passes on a multi-step prompt that emitted a plan",
            ),
        ],
        resource=resource,
    )


__all__ = ["run_profile", "suite_grade"]
