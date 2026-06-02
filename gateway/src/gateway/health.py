"""Health and readiness endpoints.

* ``/health`` — process is alive. Never touches backends.
* ``/ready`` — at least one backend is reachable for every alias.
  Probes each alias's primary (and fallback if needed) with a short
  timeout. Returns 503 + the failing aliases if anything is
  unreachable.

Both endpoints are auth-exempt so they can be used by monitoring /
curl-from-anywhere without credentials.

Phase 7.6: the per-model reachability ping moved to
:mod:`gateway.reachability` so the alias probe can run the same
check when a canary times out. ``/ready``'s response shape and
status code are unchanged — it still builds the same
``{model, reachable, detail}`` per-chain structure.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .config import Config
from .reachability import (
    DEFAULT_REACHABILITY_TIMEOUT_S,
    ReachabilityResult,
    check_reachable,
)

router = APIRouter()

# Readiness probes use a short timeout so a dead backend doesn't hang
# the endpoint. The probe doesn't run inference; it just checks the
# backend's health URL (Ollama) or a trivial auth endpoint (cloud).
_PROBE_TIMEOUT_S = DEFAULT_REACHABILITY_TIMEOUT_S


async def _probe_alias(
    client: httpx.AsyncClient, config: Config, alias: str
) -> tuple[str, bool, list[ReachabilityResult]]:
    """Probe every model in ``alias``'s resolution chain.

    Returns (alias, any_reachable, probe_results). "Ready" means at
    least one model in the chain is reachable.
    """
    chain = config.resolve_alias(alias)
    results = await asyncio.gather(*(check_reachable(client, m) for m in chain))
    return alias, any(r.reachable for r in results), list(results)


@router.get("/health")
async def health() -> dict[str, str]:
    """Process liveness. Always 200 if we can respond."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness: every configured alias has at least one reachable backend."""
    config: Config = request.app.state.config
    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
        probes = await asyncio.gather(
            *(_probe_alias(client, config, alias) for alias in config.alias_names())
        )

    failing = [alias for alias, ready, _ in probes if not ready]
    status_code = 200 if not failing else 503
    body: dict[str, Any] = {
        "status": "ok" if not failing else "degraded",
        "aliases": {
            alias: {
                "reachable": ready,
                "chain": [
                    {"model": r.model_id, "reachable": r.reachable, "detail": r.detail}
                    for r in results
                ],
            }
            for alias, ready, results in probes
        },
    }
    if failing:
        body["failing"] = failing
    return JSONResponse(status_code=status_code, content=body)
