"""Health and readiness endpoints.

* ``/health`` — process is alive. Never touches backends.
* ``/ready`` — at least one backend is reachable for every alias.
  Probes each alias's primary (and fallback if needed) with a short
  timeout. Returns 503 + the failing aliases if anything is
  unreachable.

Both endpoints are auth-exempt so they can be used by monitoring /
curl-from-anywhere without credentials.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .config import Config, ModelConfig

router = APIRouter()

# Readiness probes use a short timeout so a dead backend doesn't hang
# the endpoint. The probe doesn't run inference; it just checks the
# backend's health URL (Ollama) or a trivial auth endpoint (cloud).
_PROBE_TIMEOUT_S = 2.5


@dataclass
class _ProbeResult:
    model_id: str
    reachable: bool
    detail: str | None = None


async def _probe_model(client: httpx.AsyncClient, model: ModelConfig) -> _ProbeResult:
    """Check whether ``model``'s backend is reachable."""
    try:
        if model.backend == "ollama":
            # Ollama's /api/tags returns the list of local models; cheap
            # and gives us a real answer.
            assert model.endpoint
            r = await client.get(f"{model.endpoint.rstrip('/')}/api/tags")
            return _ProbeResult(model.id, r.status_code < 500)
        if model.backend == "openrouter":
            # GET https://openrouter.ai/api/v1/models is unauthenticated
            # and cheap. If it responds we know we have network.
            r = await client.get("https://openrouter.ai/api/v1/models")
            return _ProbeResult(model.id, r.status_code < 500)
        if model.backend == "anthropic":
            # No free ping endpoint; check that the host resolves and
            # responds. 401 here is still "reachable".
            r = await client.get("https://api.anthropic.com/v1/models")
            return _ProbeResult(model.id, r.status_code < 500)
    except (httpx.RequestError, AssertionError) as e:
        return _ProbeResult(model.id, False, detail=str(e))
    return _ProbeResult(model.id, False, detail="unknown backend")


async def _probe_alias(
    client: httpx.AsyncClient, config: Config, alias: str
) -> tuple[str, bool, list[_ProbeResult]]:
    """Probe every model in ``alias``'s resolution chain.

    Returns (alias, any_reachable, probe_results). "Ready" means at
    least one model in the chain is reachable.
    """
    chain = config.resolve_alias(alias)
    results = await asyncio.gather(*(_probe_model(client, m) for m in chain))
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
    body = {
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
