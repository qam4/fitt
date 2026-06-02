"""POST /v1/eval/<alias> — kick the alias eval harness on demand.

Phase 7 Slice 7.3: backs the ``/eval`` Telegram command and
the dashboard's per-alias "run eval" button.

The harness already exists as :func:`gateway.alias_eval.run_eval_suite`
(Phase 4.11+). This endpoint is the HTTP wrapper that calls it,
persists the report alongside the existing on-disk artifacts,
and returns a JSON summary suitable for inline rendering on
Telegram or the dashboard.

Synchronous vs async
--------------------

Sequential 5-case suite typically runs in 15-25s against a
healthy backend, longer if a binding is slow. The endpoint
runs synchronously from the caller's perspective: the bot's
``running…`` placeholder edits in the result when this
returns.

If we ever need fire-and-forget (longer suites, the dashboard
wanting to start a run and check back later), the right move
is a job-id + status-poll shape. Today's 30-60s ceiling makes
synchronous fine.

Auth
----

Bearer-gated like the rest of /v1/internal/* endpoints. POST
because the call has side effects (writes a report file under
``$FITT_HOME/eval/``).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from .alias_eval import (
    EvalCase,
    EvalReport,
    default_cases,
    render_report_markdown,
    run_eval_suite,
    write_report,
)
from .alias_eval_coding import default_coding_cases
from .config import fitt_home
from .errors import UnknownAlias
from .router import AliasRouter

router = APIRouter()
_log = logging.getLogger(__name__)


# Approx chars-per-token for the report's token-count estimate.
# Matches the 4-chars-per-token heuristic used elsewhere
# (memory.max_history_chars comments, config.example.yaml).
_CHARS_PER_TOKEN = 4


def build_realistic_system_prompt(app_state: Any) -> tuple[str, dict[str, Any]]:
    """Assemble FITT's live injected system prompt from the
    running gateway's ``app.state``, for the *realistic* eval
    suite.

    Mirrors the chat handler's assembly order
    (:func:`gateway.chat._inject_memory`): capability block,
    then skills block, then identity + lessons. Each part is
    dropped when empty so the prompt matches what live chat
    actually sends.

    Returns ``(prompt, meta)`` where ``meta`` records which
    components were present and the approximate token count —
    surfaced in the report so the verdict can say "narrated at
    5.2K tokens" like the granite write-up, and so a score is
    honest about what prompt size it measured.

    Identity + lessons are operator-specific (Principle 10);
    including them makes the eval match *this* operator's live
    prompt but means the absolute score isn't comparable across
    machines. The meta block names what was included so the
    comparison caveat is visible."""
    components: list[str] = []
    parts: list[str] = []

    registry = getattr(app_state, "tool_registry", None)
    if registry is not None and registry.list_names():
        from .capabilities import build_capability_block

        cap = build_capability_block(registry)
        if cap:
            parts.append(cap)
            components.append("capability_block")

    skills = getattr(app_state, "skills", None)
    if skills and registry is not None:
        from .skills import render_skills_block

        skills_block = render_skills_block(skills, registry)
        if skills_block:
            parts.append(skills_block)
            components.append("skills_block")

    memory = getattr(app_state, "memory", None)
    if memory is not None:
        try:
            ctx = memory.load_context("main")
        except Exception:
            ctx = None
        if ctx is not None and ctx.system_prefix:
            parts.append(ctx.system_prefix)
            components.append("identity_lessons")

    prompt = "\n\n".join(parts)
    meta = {
        "components": components,
        "chars": len(prompt),
        "approx_tokens": len(prompt) // _CHARS_PER_TOKEN,
    }
    return prompt, meta


def _summarise_report(report: EvalReport) -> dict[str, Any]:
    """JSON-friendly response shape.

    Bot / dashboard render at-a-glance pass rate plus per-case
    detail without needing to parse the markdown report. The
    markdown stays the canonical operator-readable form on
    disk; this shape is what HTTP clients consume.
    """
    return {
        "alias": report.alias,
        "model_id": report.model_id,
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "duration_ms": int((report.finished_at - report.started_at).total_seconds() * 1000),
        "passed": report.passed,
        "failed": report.failed,
        "total": report.total,
        "pass_rate": report.pass_rate,
        "cases": [
            {
                "name": c.case_name,
                "status": c.status,
                "detail": c.detail,
                "latency_ms": c.latency_ms,
                "tool_called": c.tool_called,
                "finish_reason": c.finish_reason,
            }
            for c in report.cases
        ],
    }


@router.post("/v1/eval/{alias}")
async def run_eval(
    alias: str,
    request: Request,
    suite: str = Query(
        "default",
        description=(
            "Which suite to run. 'default' is FITT's own tool-shape "
            "suite; 'coding' tests the binding under a coding-agent "
            "system prompt + read/edit/glob/shell tools."
        ),
    ),
) -> dict[str, Any]:
    """Run the requested eval suite against ``alias`` and return
    a JSON summary. Persists the report under
    ``$FITT_HOME/eval/`` (timestamped + rolling per-alias,
    namespaced by suite).

    Returns ``404`` for an unknown alias, ``400`` for an unknown
    suite. Any other failure is captured in per-case
    ``transport_error`` results — the suite never throws."""
    config = request.app.state.config

    if alias not in config.alias_names():
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "type": "unknown_alias",
                    "message": f"alias {alias!r} not configured",
                    "available": config.alias_names(),
                }
            },
        )

    # The runner needs an AliasRouter. We construct one fresh
    # per call rather than reusing app.state because the chat
    # path's router is wrapped in middleware we don't want
    # interfering with the eval's request shape.
    eval_router = AliasRouter(config)

    # Pick the suite. Unknown names get a 400 — operator typo
    # is the most common reason this fails, and a clear error
    # is better than silently running the default.
    realistic_prompt = ""
    realistic_meta: dict[str, Any] = {}
    if suite == "default":
        cases: list[EvalCase] = default_cases()
    elif suite == "coding":
        cases = default_coding_cases()
    elif suite == "realistic":
        # Realistic = the default cases (FITT's own tool names)
        # run under FITT's live injected system prompt. The
        # diff between the 'default' suite (bare prompt) and
        # this one is the granite-incident diagnostic.
        cases = default_cases()
        realistic_prompt, realistic_meta = build_realistic_system_prompt(request.app.state)
    else:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "type": "unknown_suite",
                    "message": f"suite {suite!r} not recognized",
                    "available": ["default", "coding", "realistic"],
                }
            },
        )
    try:
        report = await run_eval_suite(
            alias, eval_router, cases=cases, system_prompt=realistic_prompt
        )
    except UnknownAlias as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "type": "unknown_alias",
                    "message": str(exc),
                    "available": exc.available,
                }
            },
        ) from None
    except Exception as exc:
        # The suite catches dispatch failures per case; this
        # branch only fires on infrastructure failures (a bug
        # in the harness itself, an env issue). Log and
        # surface a 500 so the operator notices.
        _log.exception("eval.suite_failed", extra={"alias": alias})
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "type": "eval_infrastructure_failure",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            },
        ) from exc

    # Persist alongside the existing on-disk eval artifacts so
    # ``/v1/aliases``'s last_eval lookup picks it up on the
    # next call.
    extra_header_lines: list[str] = []
    if suite == "realistic":
        approx = realistic_meta.get("approx_tokens", 0)
        comps = ", ".join(realistic_meta.get("components", [])) or "(none)"
        extra_header_lines.append(
            f"- Realistic prompt: ~{approx} tokens "
            f"({realistic_meta.get('chars', 0)} chars; components: {comps})"
        )
    try:
        write_report(report, fitt_home(), suite=suite, extra_header_lines=extra_header_lines)
    except OSError as exc:
        _log.warning(
            "eval.write_report_failed",
            extra={"alias": alias, "suite": suite, "error": f"{type(exc).__name__}: {exc}"},
        )

    summary = _summarise_report(report)
    summary["suite"] = suite
    if suite == "realistic":
        summary["realistic_prompt"] = realistic_meta
    # Include the rendered markdown so a dashboard or CLI can
    # show the human-readable form without re-rendering.
    summary["markdown"] = render_report_markdown(report, extra_header_lines=extra_header_lines)
    return summary
