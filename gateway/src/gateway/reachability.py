"""Backend reachability ping (Phase 7.6).

A cheap, no-inference check of whether a model's backend is
reachable: ``GET /api/tags`` for Ollama, ``GET /v1/models`` for
OpenRouter, ``GET /v1/models`` for Anthropic (a 401 there still
means "reachable" — the host answered). This is the mechanism
``/ready`` (``health.py``) has used since Phase 1; Phase 7.6
extracts it here so the alias probe can run the same check when
a canary times out, to tell "host is slow / cold-loading"
(reachable) apart from "host is down" (unreachable).

``health.py`` imports :func:`check_reachable` and keeps its
``/ready`` response shape and status code unchanged — the
extraction is a pure refactor on that side.

Why a separate, faster timeout than the inference probe
-------------------------------------------------------

This ping must be *fast* — it's a liveness check, not a
generation. A 2.5s budget is plenty for ``/api/tags`` to answer
on a healthy host and short enough that a dead host fails the
check quickly. It is deliberately distinct from the alias
probe's inference timeout (``boot_probe_timeout_s``, 10s+),
which has to cover model cold-load + full generation. The two
knobs measure different things and are not unified.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

import httpx

from .config import ModelConfig

# Reachability pings use a short timeout so a dead backend
# doesn't hang the caller. Matches the value /ready used as
# ``_PROBE_TIMEOUT_S`` before the extraction.
DEFAULT_REACHABILITY_TIMEOUT_S = 2.5


@dataclass(frozen=True, slots=True)
class ReachabilityResult:
    """Outcome of a single backend reachability ping.

    ``reachable`` is the verdict; ``latency_ms`` is how long the
    ping took (useful context even on success); ``detail`` carries
    the failure reason when unreachable (the httpx error string,
    or "unknown backend")."""

    model_id: str
    reachable: bool
    latency_ms: int
    detail: str | None = None


async def check_reachable(
    client: httpx.AsyncClient,
    model: ModelConfig,
    *,
    timeout_s: float = DEFAULT_REACHABILITY_TIMEOUT_S,
) -> ReachabilityResult:
    """Check whether ``model``'s backend is reachable.

    No inference — just a cheap GET against a known endpoint.
    Never raises; transport failures become
    ``reachable=False`` results with the error in ``detail``.

    The ``client``'s own timeout governs the request; pass a
    client constructed with ``timeout=timeout_s`` (see
    :func:`check_reachable_standalone` for the construct-and-ping
    convenience wrapper). ``timeout_s`` is recorded for callers
    that want to surface the budget.
    """
    started = perf_counter()

    def _elapsed_ms() -> int:
        return int((perf_counter() - started) * 1000)

    try:
        if model.backend == "ollama":
            # Ollama's /api/tags returns the list of local models;
            # cheap and gives us a real answer.
            assert model.endpoint
            r = await client.get(f"{model.endpoint.rstrip('/')}/api/tags")
            return ReachabilityResult(model.id, r.status_code < 500, _elapsed_ms())
        if model.backend == "openrouter":
            # GET https://openrouter.ai/api/v1/models is
            # unauthenticated and cheap. If it responds we know
            # we have network.
            r = await client.get("https://openrouter.ai/api/v1/models")
            return ReachabilityResult(model.id, r.status_code < 500, _elapsed_ms())
        if model.backend == "anthropic":
            # No free ping endpoint; check that the host resolves
            # and responds. 401 here is still "reachable".
            r = await client.get("https://api.anthropic.com/v1/models")
            return ReachabilityResult(model.id, r.status_code < 500, _elapsed_ms())
        if model.backend == "openai":
            # Generic OpenAI-compatible backend (Nvidia NIM, Groq,
            # vLLM, ...). Probe the configured endpoint's /models.
            # Phase 7.6: /ready never covered this backend (it
            # predated the openai backend's wider use); the probe
            # path benefits from it, so add the case here. Any
            # response < 500 (including 401) means "reachable".
            if model.endpoint:
                r = await client.get(f"{model.endpoint.rstrip('/')}/models")
                return ReachabilityResult(model.id, r.status_code < 500, _elapsed_ms())
    except (httpx.RequestError, AssertionError) as e:
        return ReachabilityResult(model.id, False, _elapsed_ms(), detail=str(e))
    return ReachabilityResult(model.id, False, _elapsed_ms(), detail="unknown backend")


async def check_reachable_standalone(
    model: ModelConfig,
    *,
    timeout_s: float = DEFAULT_REACHABILITY_TIMEOUT_S,
) -> ReachabilityResult:
    """Construct a short-lived client and ping ``model`` once.

    Convenience for callers that don't already hold an
    ``httpx.AsyncClient`` — notably the alias probe, which runs
    a one-off reachability check after a canary times out. The
    ``/ready`` endpoint shares one client across the whole alias
    set, so it uses :func:`check_reachable` directly.
    """
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        return await check_reachable(client, model, timeout_s=timeout_s)
