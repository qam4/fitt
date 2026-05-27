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
    if suite == "default":
        cases: list[EvalCase] = default_cases()
    elif suite == "coding":
        cases = default_coding_cases()
    else:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "type": "unknown_suite",
                    "message": f"suite {suite!r} not recognized",
                    "available": ["default", "coding"],
                }
            },
        )
    try:
        report = await run_eval_suite(alias, eval_router, cases=cases)
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
    try:
        write_report(report, fitt_home(), suite=suite)
    except OSError as exc:
        _log.warning(
            "eval.write_report_failed",
            extra={"alias": alias, "suite": suite, "error": f"{type(exc).__name__}: {exc}"},
        )

    summary = _summarise_report(report)
    summary["suite"] = suite
    # Include the rendered markdown so a dashboard or CLI can
    # show the human-readable form without re-rendering.
    summary["markdown"] = render_report_markdown(report)
    return summary
