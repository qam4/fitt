"""POST /v1/probe/<alias> — run the tool-call canary on demand.

Phase 7.6 Decision 4: the manual companion to sequential
per-endpoint probing. Eval has been per-alias since Phase 7.3
(``POST /v1/eval/<alias>``); the probe had a per-alias function
(:func:`gateway.alias_probe.probe_alias`) but only ever ran it
via the boot batch. This endpoint exposes the per-alias
capability so an operator debugging one binding can probe *just*
that one — it gets the backend to itself, returns a clean
result, and doesn't disturb the siblings or wait for a full
sweep.

Why per-alias matters on a shared GPU
-------------------------------------

When three aliases share one laptop's Ollama, a re-probe-all
(even the now-sequential one) cold-loads each model in turn —
30-60s for 14B-class models. If you only changed one binding,
that's wasted time and VRAM churn. ``POST /v1/probe/<alias>``
probes the one you care about: one cold-load, one result.

Auth
----

Bearer-gated like ``/v1/eval/<alias>``. POST because it has a
side effect — it updates ``app.state.alias_probe_results`` so
the dashboard's "last probe" reflects the fresh run.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .alias_probe import probe_alias
from .router import AliasRouter

router = APIRouter()
_log = logging.getLogger(__name__)


def _summarise_probe(result: Any) -> dict[str, Any]:
    """JSON-friendly response shape mirroring the eval endpoint's
    ``_summarise_report`` style: a flat dict the dashboard / CLI
    render without re-deriving anything."""
    return {
        "alias": result.alias,
        "status": result.status,
        "detail": result.detail,
        "latency_ms": result.latency_ms,
        "model_used": result.model_used,
        "finish_reason": result.finish_reason,
        "reply_preview": result.reply_preview,
        "reachable": result.reachable,
    }


@router.post("/v1/probe/{alias}")
async def run_probe(alias: str, request: Request) -> dict[str, Any]:
    """Run the tool-call canary against ``alias`` and return the
    :class:`gateway.alias_probe.ProbeResult` as JSON. Updates
    ``app.state.alias_probe_results[alias]`` so the dashboard's
    last-probe view reflects the fresh run.

    Returns ``404`` for an unknown alias. The probe itself never
    raises — any dispatch failure becomes a classified status in
    the result body (``upstream_silent`` / ``unreachable`` /
    ...), so a successful HTTP 200 can still carry a non-``ok``
    probe status."""
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

    # Fresh router per call — same reasoning as the eval endpoint:
    # the chat path's router is wrapped in middleware we don't
    # want interfering with the canary's request shape.
    probe_router = AliasRouter(config)
    result = await probe_alias(
        alias,
        probe_router,
        timeout_s=config.server.boot_probe_timeout_s,
        config=config,
    )

    # Persist so the dashboard / /v1/aliases last-probe surfaces
    # the fresh result. Mirrors the boot probe's app.state write.
    results = getattr(request.app.state, "alias_probe_results", None)
    if isinstance(results, dict):
        results[alias] = result
    else:
        request.app.state.alias_probe_results = {alias: result}
    request.app.state.alias_probe_ran_at = time.time()

    _log.info(
        "probe.on_demand",
        extra={"alias": alias, "status": result.status, "latency_ms": result.latency_ms},
    )

    return _summarise_probe(result)
