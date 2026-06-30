"""POST /v1/profile/<alias> — build a capability profile on demand.

Phase 12.5a. The profiler existed only as the ``fitt profile
alias`` CLI; this endpoint exposes it so the dashboard can
trigger a run (the probe and eval are already dashboard-runnable;
the profile was the odd one out, which is why a home box showed
"No capability profile on disk" — the CLI wrote to the host's
``~/.fitt`` while the containerised gateway read its own
``FITT_HOME``).

Because the gateway runs it, the profile is written under the
gateway's own ``$FITT_HOME/eval/`` — the same directory the
dashboard reads — so a run is always visible to the dashboard
that triggered it.

Synchronous, like ``/v1/eval``: the profiler runs the eval suites
+ plan-election (minutes on a slow model) and the caller waits.
Bearer-gated; POST because it writes a report file.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from .capability_profile import (
    load_baseline,
    profile_to_dict,
    render_diff_markdown,
    render_profile_markdown,
    write_profile,
)
from .config import fitt_home
from .profile_runner import run_profile

router = APIRouter()
_log = logging.getLogger(__name__)


@router.post("/v1/profile/{alias}")
async def run_profile_endpoint(
    alias: str,
    request: Request,
    samples: int = Query(5, ge=1, description="Multi-sample count per case."),
    timeout: float = Query(30.0, gt=0, description="Per-case dispatch timeout (s)."),
) -> dict[str, Any]:
    """Build a capability profile for ``alias`` and return it as
    JSON. Persists ``<alias>-profile.{md,json}`` under the
    gateway's ``$FITT_HOME/eval/`` (the dir the dashboard reads).
    Returns 404 for an unknown alias; 500 only on an
    infrastructure failure in the run itself."""
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

    # Load the baseline BEFORE the run writes (write overwrites it).
    baseline = load_baseline(alias, fitt_home())
    try:
        profile = await run_profile(
            alias=alias,
            cfg=config,
            state=request.app.state,
            samples=samples,
            timeout_s=timeout,
        )
    except Exception as exc:
        _log.exception("profile.run_failed", extra={"alias": alias})
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "type": "profile_infrastructure_failure",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            },
        ) from exc

    try:
        write_profile(profile, fitt_home())
    except OSError as exc:
        _log.warning(
            "profile.write_failed",
            extra={"alias": alias, "error": f"{type(exc).__name__}: {exc}"},
        )

    out = profile_to_dict(profile)
    out["markdown"] = render_profile_markdown(profile)
    if baseline is not None:
        out["diff_markdown"] = render_diff_markdown(profile.diff(baseline))
    return out
