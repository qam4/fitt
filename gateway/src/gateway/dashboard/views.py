"""Per-page view handlers for the dashboard.

Each view follows the same shape:

1. Run :func:`gateway.dashboard.auth.authorize_request` first.
2. Read in-process state from ``request.app.state`` (no
   network calls; the gateway endpoints already aggregate the
   data we want).
3. Massage into a context dict.
4. Render a Jinja template against ``base.html``.

The same data the Telegram ``/status`` and ``/lastturn``
commands surface is what lands here, just rendered in a
browser-friendly form. We deliberately don't add a parallel
data path; if the dashboard wants something the existing
endpoints don't expose, the right move is to extend the
endpoint, not to read on-disk files behind the gateway's
back. See design.md decision D5.

The overview view ships first; subsequent slice-7.5 commits
land per-page views (aliases, turns, tools, cron, audit,
health, gaps) in this same module.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from ..config import fitt_home as _fitt_home
from .auth import authorize_request

# --------------------------------------------------------------- templates


_TEMPLATE_DIR = Path(__file__).parent / "templates"
"""Resolved at import time. The dashboard ships its templates
inside the package so a wheel install / docker layer carries
them automatically."""


def _make_templates() -> Jinja2Templates:
    """Construct the Jinja2 environment. Factored out so tests
    that need a fresh environment can reach in if they want;
    production calls this once via :data:`templates`."""
    return Jinja2Templates(directory=str(_TEMPLATE_DIR))


templates = _make_templates()


# --------------------------------------------------------------- helpers


def _fmt_duration(seconds: float | None) -> str:
    """Human-friendly compact duration. ``2h 13m``, ``45s``,
    ``3d 4h``. Mirrors the bot's status formatter so operators
    who switch surfaces see consistent units."""
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m {s}s" if s else f"{m}m"
    if seconds < 86400:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}m" if m else f"{h}h"
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    return f"{d}d {h}h" if h else f"{d}d"


def _fmt_age(ts: float | None) -> str:
    """Render a UNIX timestamp as ``Xh ago`` / ``Xd ago``.
    ``never`` for ``None``."""
    if ts is None:
        return "never"
    delta = max(0.0, time.time() - ts)
    return f"{_fmt_duration(delta)} ago"


def _fmt_iso(ts: float | None) -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_tokens(n: int | None) -> str:
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n // 1000}k"
    return str(n)


def _read_anchor_ts(path: Path) -> float | None:
    """Mirror of :func:`gateway.status_endpoint._read_anchor_ts`.
    Kept private here so the dashboard can read pruner cadence
    state without importing from the endpoint module."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
        return float(text) if text else None
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------- overview


def _build_overview_context(request: Request) -> dict[str, Any]:
    """Assemble every field the overview templates render.

    Reads in-process state — same data the ``/v1/aliases`` /
    ``/v1/status`` endpoints expose, just consumed directly so
    the browser doesn't need to make extra round-trips. The
    aggregation cost is ~0 on the request hot path."""
    app = request.app
    config = app.state.config
    home = _fitt_home()
    now = time.time()

    # --- gateway uptime --------------------------------------
    started_at: float | None = getattr(app.state, "started_at", None)
    if started_at is None:
        started_at = now
    uptime_s = max(0.0, now - started_at)

    # --- alias rows ------------------------------------------
    cache = getattr(app.state, "context_windows", None)
    probe_results: dict[str, Any] = getattr(app.state, "alias_probe_results", {}) or {}
    alias_rows: list[dict[str, Any]] = []
    alias_ok_count = 0
    for alias in config.alias_names():
        chain = config.resolve_alias(alias)
        primary = chain[0]
        cw_tokens: int | None = None
        if cache is not None:
            cw = cache.get(primary.backend, primary.id)
            if cw is not None and cw.tokens is not None:
                cw_tokens = cw.tokens

        probe = probe_results.get(alias)
        if probe is not None and probe.status == "ok":
            pip = "ok"
            alias_ok_count += 1
            last_probe_text = "ok"
        elif probe is not None and probe.status == "skipped_no_api_key":
            pip = "warn"
            last_probe_text = "skipped — missing api_key"
        elif probe is not None:
            pip = "error"
            last_probe_text = probe.status
        else:
            pip = "unknown"
            last_probe_text = "not probed"

        alias_rows.append(
            {
                "id": alias,
                "model": primary.model,
                "backend": primary.backend,
                "context_window_human": (_fmt_tokens(cw_tokens) if cw_tokens is not None else "?"),
                "pip": pip,
                "last_probe_text": last_probe_text,
            }
        )

    # --- mcp -------------------------------------------------
    mcp_servers = []
    mcp_manager = getattr(app.state, "mcp", None)
    if mcp_manager is not None:
        try:
            mcp_servers = list(mcp_manager.describe() or [])
        except Exception:
            mcp_servers = []
    mcp_running = sum(1 for s in mcp_servers if s.get("running"))
    mcp_total = len(mcp_servers)

    # --- cron ------------------------------------------------
    cron_total = 0
    cron_enabled = 0
    cron_service = getattr(app.state, "cron", None)
    if cron_service is not None:
        try:
            jobs = cron_service.list(include_disabled=True)
            cron_total = len(jobs)
            cron_enabled = sum(1 for j in jobs if getattr(j, "enabled", True))
        except Exception:
            pass

    # --- gaps + pruners + telegram ---------------------------
    gap_count = 0
    gap_log = getattr(app.state, "capability_gaps", None)
    if gap_log is not None:
        try:
            gap_count = len(gap_log.read())
        except Exception:
            pass

    history_last_sweep = _read_anchor_ts(home / "history.pruner.anchor")
    event_last_sweep = _read_anchor_ts(home / "events.pruner.anchor")

    secrets = getattr(config, "secrets", None)
    telegram_configured = bool(secrets and getattr(secrets, "telegram", None))

    return {
        "uptime_human": _fmt_duration(uptime_s),
        "started_at_human": _fmt_iso(started_at),
        "alias_count": len(alias_rows),
        "alias_ok_count": alias_ok_count,
        "alias_rows": alias_rows,
        "mcp_running": mcp_running,
        "mcp_total": mcp_total,
        "cron_total": cron_total,
        "cron_enabled": cron_enabled,
        "gap_count": gap_count,
        "history_pruner_text": _fmt_age(history_last_sweep),
        "event_pruner_text": _fmt_age(event_last_sweep),
        "telegram_text": "configured" if telegram_configured else "not configured",
        "generated_at_human": datetime.fromtimestamp(now, tz=UTC).strftime("%H:%M:%S UTC"),
    }


def _stub_view_context(*, title: str, description: str) -> dict[str, Any]:
    """Render context for the placeholder pages still pending in
    Slice 7.5. Each links forward to its own task in tasks.md so
    the operator who clicks early sees what's missing instead of
    a 404."""
    return {"title": title, "description": description}


def build_views_router() -> APIRouter:
    """Build the views sub-router. Mounted under ``/dashboard``
    by :func:`gateway.dashboard.router.build_router`."""
    router = APIRouter()

    # --------------------------------------------------------- overview

    @router.get("/", response_class=HTMLResponse)
    async def root(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_overview_context(request)
        ctx["client"] = getattr(request.state, "client", "webui")
        return templates.TemplateResponse(request, "overview.html", ctx)

    @router.get("/_partials/overview", response_class=HTMLResponse)
    async def overview_partial(request: Request) -> Response:
        """HTMX-driven refresh fragment. Re-renders just the
        ``_overview_panel.html`` chunk every 30s."""
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_overview_context(request)
        return templates.TemplateResponse(
            request,
            "_overview_panel.html",
            ctx,
        )

    # --------------------------------------------------------- placeholders

    @router.get("/aliases", response_class=HTMLResponse)
    async def aliases_view(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        # Placeholder until Task 25 lands.
        return _placeholder(request, page="aliases", title="Aliases")

    @router.get("/turns", response_class=HTMLResponse)
    @router.get("/turns/{session}", response_class=HTMLResponse)
    @router.get("/turns/{session}/{turn_id}", response_class=HTMLResponse)
    async def turns_view(
        request: Request,
        session: str | None = None,
        turn_id: str | None = None,
    ) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        return _placeholder(request, page="turns", title="Turns")

    @router.get("/tools", response_class=HTMLResponse)
    async def tools_view(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        return _placeholder(request, page="tools", title="Tools")

    @router.get("/cron", response_class=HTMLResponse)
    async def cron_view(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        return _placeholder(request, page="cron", title="Cron")

    @router.get("/audit", response_class=HTMLResponse)
    async def audit_view(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        return _placeholder(request, page="audit", title="Audit")

    @router.get("/health", response_class=HTMLResponse)
    async def health_view(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        return _placeholder(request, page="health", title="Health")

    @router.get("/gaps", response_class=HTMLResponse)
    async def gaps_view(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        return _placeholder(request, page="gaps", title="Capability gaps")

    return router


def _placeholder(request: Request, *, page: str, title: str) -> Response:
    """Render the placeholder template for views still pending
    later Slice 7.5 commits. Better UX than a 404 for the
    operator who clicks the sidebar links early."""
    client = getattr(request.state, "client", "webui")
    return templates.TemplateResponse(
        request,
        "placeholder.html",
        {
            "client": client,
            "nav_active": page,
            "title": title,
        },
    )
