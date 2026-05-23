"""GET /v1/status — operator-facing system snapshot (Phase 7 Slice 7.3).

Aggregates the small per-subsystem questions an operator wants
answered when they ask "is FITT okay right now?" — without
ssh'ing into the hub or grepping six logs.

Reads from existing in-process state:

* Gateway uptime (recorded at app construction).
* MCP server count + running / down status (from the manager).
* Cron jobs (count + next firing).
* Capability-gap log size (count of recorded gaps).
* History pruner / event pruner last-sweep timestamps (from the
  anchor files when present).

What this endpoint deliberately doesn't include
-----------------------------------------------

* **Per-alias readiness probes.** ``/ready`` already does that
  and runs real network calls; conflating with a fast snapshot
  endpoint would defeat the latency contract. Operators wanting
  reachability hit ``/ready``; this endpoint is the snappy
  "what's going on" view.
* **Detailed subsystem state.** The endpoint surfaces counts and
  high-level flags, not the specifics. ``/v1/mcp``,
  ``/v1/aliases``, ``/v1/cron`` (when it exists) carry the
  drill-down.

Bearer-gated. Read-only. Used by the Telegram ``/status``
command and the dashboard's overview / health views.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from .config import fitt_home

router = APIRouter()


def _read_anchor_ts(path: Path) -> float | None:
    """Per-pruner anchor files store a single float timestamp on
    disk so the cadence survives gateway restarts. Reading the
    file gives us the last-sweep time without the pruner having
    to expose internal state."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
        return float(text) if text else None
    except (OSError, ValueError):
        return None


def _next_firing_ts(jobs: list[Any], *, now: float) -> float | None:
    """Return the soonest *next* firing across the enabled jobs,
    or ``None`` when nothing is scheduled.

    CronJob doesn't cache a ``next_firing`` value; we compute it
    on demand from the job's schedule. Failures (a malformed
    cron expression, a consumed one-shot) drop the job from the
    aggregation rather than crashing the endpoint."""
    soonest: float | None = None
    for job in jobs:
        schedule = getattr(job, "schedule", None)
        if schedule is None:
            continue
        try:
            nxt = schedule.next_run(after=now)
        except Exception:
            continue
        if nxt is None:
            continue
        if soonest is None or nxt < soonest:
            soonest = float(nxt)
    return soonest


@router.get("/v1/status")
async def status(request: Request) -> dict[str, Any]:
    """System-level snapshot.

    Cheap to call (no network, no probing). Renderers (Telegram
    /status command, dashboard overview) call this on every
    refresh."""
    app = request.app
    home = fitt_home()

    # --- gateway -----------------------------------------------
    started_at: float | None = getattr(app.state, "started_at", None)
    if started_at is None:
        # The first request sets the timestamp lazily so we
        # don't have to thread it through create_app. Subsequent
        # requests reuse it.
        app.state.started_at = time.time()
        started_at = app.state.started_at
    uptime_s = max(0.0, time.time() - started_at)

    # --- mcp ---------------------------------------------------
    mcp_servers: list[dict[str, Any]] = []
    mcp_manager = getattr(app.state, "mcp", None)
    if mcp_manager is not None:
        try:
            mcp_servers = list(mcp_manager.describe() or [])
        except Exception:
            mcp_servers = []
    mcp_running = sum(1 for s in mcp_servers if s.get("running"))
    mcp_total = len(mcp_servers)

    # --- cron --------------------------------------------------
    cron_total = 0
    cron_enabled = 0
    cron_next_firing: float | None = None
    cron_service = getattr(app.state, "cron", None)
    if cron_service is not None:
        try:
            jobs = cron_service.list(include_disabled=True)
            cron_total = len(jobs)
            enabled_jobs = [j for j in jobs if getattr(j, "enabled", True)]
            cron_enabled = len(enabled_jobs)
            cron_next_firing = _next_firing_ts(enabled_jobs, now=time.time())
        except Exception:
            pass

    # --- capability gaps ---------------------------------------
    gap_count = 0
    gap_log = getattr(app.state, "capability_gaps", None)
    if gap_log is not None:
        try:
            gap_count = len(gap_log.read())
        except Exception:
            pass

    # --- pruners -----------------------------------------------
    history_anchor = home / "history.pruner.anchor"
    event_anchor = home / "events.pruner.anchor"
    history_last_sweep = _read_anchor_ts(history_anchor)
    event_last_sweep = _read_anchor_ts(event_anchor)

    # --- telegram bot present? --------------------------------
    # The gateway can't introspect the bot directly (separate
    # process), but operator-facing "is the bot configured" is
    # the secret presence question. Match the existing helper.
    config = app.state.config
    secrets = getattr(config, "secrets", None)
    telegram_configured = bool(secrets and getattr(secrets, "telegram", None))

    return {
        "generated_at": time.time(),
        "gateway": {
            "uptime_s": uptime_s,
            "started_at": started_at,
        },
        "mcp": {
            "servers_total": mcp_total,
            "servers_running": mcp_running,
        },
        "cron": {
            "total": cron_total,
            "enabled": cron_enabled,
            "next_firing": cron_next_firing,
        },
        "capability_gaps": {
            "total": gap_count,
        },
        "pruners": {
            "history_last_sweep": history_last_sweep,
            "events_last_sweep": event_last_sweep,
        },
        "telegram": {
            "configured": telegram_configured,
        },
    }
