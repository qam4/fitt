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

import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from ..alias_eval import default_eval_dir
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


# --------------------------------------------------------------- aliases


# Mirrors the regex in :mod:`gateway.aliases_endpoint`. Kept as a
# private duplicate here because the endpoint module's parser is
# closed over file IO and a regex compile cost we don't want to
# import for every dashboard render.
_EVAL_RESULT_RE = re.compile(
    r"^-\s+Result:\s+\*\*(?P<passed>\d+)/(?P<total>\d+)\s+passed\*\*\s+\((?P<pct>\d+)%\)"
)
_EVAL_FINISHED_RE = re.compile(r"^-\s+Finished:\s+(?P<iso>\S+)")


def _parse_eval_report(path: Path) -> dict[str, Any] | None:
    """Read the rolling per-alias eval report header. Returns the
    summary dict or ``None`` when the file's missing / unparseable.
    Same parser the ``/v1/aliases`` endpoint uses; duplicated
    locally to avoid the cross-module import for the dashboard's
    hot path."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    passed: int | None = None
    total: int | None = None
    finished_iso: str | None = None
    for line in text.splitlines()[:30]:
        m = _EVAL_RESULT_RE.match(line)
        if m is not None:
            passed = int(m.group("passed"))
            total = int(m.group("total"))
            continue
        m = _EVAL_FINISHED_RE.match(line)
        if m is not None:
            finished_iso = m.group("iso")
        if passed is not None and finished_iso is not None:
            break
    if passed is None or total is None:
        return None
    return {
        "passed": passed,
        "total": total,
        "pass_rate": passed / total if total > 0 else 0.0,
        "finished_iso": finished_iso,
    }


def _format_eval_cell(report: dict[str, Any] | None) -> str:
    """Render the last-eval column. Returns HTML so we can include
    a colour-coded pass-rate badge. Caller marks the value safe in
    the template."""
    if report is None:
        return '<span class="dim">—</span>'
    passed = report["passed"]
    total = report["total"]
    pct = round(report["pass_rate"] * 100)
    if pct >= 90:
        cls = "badge"
    elif pct >= 60:
        cls = "badge-warn"
    else:
        cls = "badge-error"
    return f'<span class="{cls}">{passed}/{total} ({pct}%)</span>'


def _count_dispatches_last_24h(audit: Any) -> dict[str, int]:
    """Walk the audit log and count tool-call entries per alias
    over the last 24 hours.

    The audit log doesn't carry an alias field directly — what
    it does carry is the tool name (and ``extra`` payloads when
    the tool registered them). Per-alias dispatch volume isn't
    something the audit log surfaces today. For the v0 aliases
    view we approximate via the ``audit.extra.alias`` field
    where present, falling back to zero counts when absent.

    A future commit will land per-turn dispatch metadata in the
    audit log so this aggregation stops being best-effort. The
    placeholder shape keeps the dashboard honest about what
    it knows."""
    counts: dict[str, int] = {}
    if audit is None:
        return counts
    cutoff = time.time() - 86400
    try:
        entries = audit.iter_entries()
    except Exception:
        return counts
    for entry in entries:
        try:
            ts = float(entry.get("ts", 0.0))
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            continue
        extra = entry.get("extra") or {}
        alias = extra.get("alias") if isinstance(extra, dict) else None
        if isinstance(alias, str) and alias:
            counts[alias] = counts.get(alias, 0) + 1
    return counts


def _build_aliases_context(request: Request) -> dict[str, Any]:
    """Same alias enumeration as the overview but with the full
    detail set the aliases view renders: fallback model id,
    discovery source, last-eval summary, recent dispatch count.
    """
    app = request.app
    config = app.state.config
    home = _fitt_home()
    now = time.time()

    cache = getattr(app.state, "context_windows", None)
    probe_results: dict[str, Any] = getattr(app.state, "alias_probe_results", {}) or {}
    audit = getattr(app.state, "audit", None)
    dispatch_counts = _count_dispatches_last_24h(audit)
    eval_dir = default_eval_dir(home)

    alias_rows: list[dict[str, Any]] = []
    for alias in config.alias_names():
        chain = config.resolve_alias(alias)
        primary = chain[0]
        fallback = chain[1] if len(chain) > 1 else None

        cw_tokens: int | None = None
        cw_source = "—"
        if cache is not None:
            cw = cache.get(primary.backend, primary.id)
            if cw is not None:
                cw_tokens = cw.tokens
                cw_source = cw.source

        probe = probe_results.get(alias)
        if probe is not None and probe.status == "ok":
            pip = "ok"
            last_probe_text = "ok"
        elif probe is not None and probe.status == "skipped_no_api_key":
            pip = "warn"
            last_probe_text = "skipped — no api_key"
        elif probe is not None:
            pip = "error"
            last_probe_text = probe.status
        else:
            pip = "unknown"
            last_probe_text = "not probed"

        eval_report = _parse_eval_report(eval_dir / f"{alias}-latest.md")

        alias_rows.append(
            {
                "id": alias,
                "model": primary.model,
                "fallback": fallback.model if fallback else None,
                "backend": primary.backend,
                "context_window_human": (_fmt_tokens(cw_tokens) if cw_tokens is not None else "?"),
                "context_source": cw_source,
                "pip": pip,
                "last_probe_text": last_probe_text,
                "last_eval_text": _format_eval_cell(eval_report),
                "dispatched_24h": dispatch_counts.get(alias, 0),
            }
        )

    return {
        "alias_rows": alias_rows,
        "generated_at_human": datetime.fromtimestamp(now, tz=UTC).strftime("%H:%M:%S UTC"),
    }


# --------------------------------------------------------------- turns


def _safe_session_key(s: str) -> str:
    """Reject anything that doesn't look like a valid session id —
    a path-traversal attempt via the URL would otherwise let an
    operator browse arbitrary directories under sessions/.
    Mirror of :data:`gateway.sessions.SESSION_ID_PATTERN`'s
    intent: lowercase letters, digits, hyphens.
    """
    if not s:
        return "main"
    cleaned = "".join(c for c in s if c.isalnum() or c in "-_")
    return cleaned[:64] or "main"


def _format_message_preview(content: Any) -> str:
    """First-line preview for the ``dispatched_messages`` list.
    Each capture stores the OpenAI-shape ``content`` field, which
    can be a string, a list of content parts, or None for tool
    messages."""
    if content is None:
        return "(no content)"
    if isinstance(content, str):
        flat = content.replace("\n", " ").strip()
        return flat[:120] + ("…" if len(flat) > 120 else "")
    if isinstance(content, list):
        # OpenAI multi-part content. Concatenate text parts.
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        flat = " ".join(parts).replace("\n", " ").strip()
        return flat[:120] + ("…" if len(flat) > 120 else "")
    return str(content)[:120]


def _format_message_full(content: Any) -> str:
    """Renderable form for the ``<pre>`` body. Strings are passed
    through; multi-part content is flattened with one part per
    line so the structure stays visible."""
    if content is None:
        return "(no content)"
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out_lines: list[str] = []
        for part in content:
            if isinstance(part, dict):
                ptype = part.get("type", "?")
                if ptype == "text":
                    out_lines.append(str(part.get("text", "")))
                else:
                    out_lines.append(f"[{ptype}] {part!r}")
            else:
                out_lines.append(str(part))
        return "\n\n".join(out_lines)
    return str(content)


def _flatten_response_text(response: dict[str, Any]) -> str:
    """Pull the assistant text out of a LiteLLM response dict.
    Returns ``""`` when the response had no choices or only tool
    calls. Best-effort — the dashboard renders the full JSON in a
    collapsed pre block separately, so even an unparseable response
    doesn't break the view."""
    try:
        choices = response.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        text = msg.get("content")
        if text is None:
            return ""
        if isinstance(text, str):
            return text
        return str(text)
    except (AttributeError, IndexError, TypeError):
        return ""


def _list_capture_sessions(sessions_dir: Path) -> list[str]:
    """Walk the sessions/ directory looking for any session that
    has a ``turns/`` subdirectory with at least one capture file.
    Best-effort: a missing or unreadable sessions dir returns []."""
    if not sessions_dir.exists():
        return []
    try:
        candidates = [p for p in sessions_dir.iterdir() if p.is_dir()]
    except OSError:
        return []
    out: list[str] = []
    for sd in candidates:
        turns_dir = sd / "turns"
        if not turns_dir.exists():
            continue
        try:
            has_capture = any(d.is_dir() for d in turns_dir.iterdir())
        except OSError:
            continue
        if has_capture:
            out.append(sd.name)
    out.sort(key=lambda s: (s != "main", s))
    return out


def _build_turn_list_context(
    request: Request, *, session_key: str, limit: int = 50
) -> dict[str, Any]:
    """Assemble the turns-list page context from the capture
    store. The store handles the per-day directory walk; we
    annotate each summary dict with display-ready fields."""
    store = getattr(request.app.state, "turn_capture", None)
    if store is None:
        return {
            "session_key": session_key,
            "turns": [],
            "limit": limit,
            "available_sessions": [],
        }
    raw = store.list_recent(session_key, limit=limit)
    turns: list[dict[str, Any]] = []
    for s in raw:
        started = float(s.get("started_at", 0.0))
        finished = float(s.get("finished_at", started))
        turns.append(
            {
                "turn_id": s["turn_id"],
                "started_iso": _fmt_iso(started),
                "age_human": _fmt_age(started),
                "alias": s.get("alias", "?"),
                "model_used": s.get("model_used", "?"),
                "prompt_human": _fmt_tokens(s.get("prompt_tokens")),
                "fill_human": (
                    f"{s['prompt_pct_of_window']:.0f}%"
                    if s.get("prompt_pct_of_window") is not None
                    else "—"
                ),
                "tool_calls_count": s.get("tool_calls_count", 0),
                "finish_reason": s.get("finish_reason"),
                "narration_warning": bool(s.get("narration_warning")),
                "status": s.get("status", "ok"),
                "latency_ms": int(max(0.0, (finished - started) * 1000)),
            }
        )

    config = request.app.state.config
    available = _list_capture_sessions(config.memory.sessions_dir)
    return {
        "session_key": session_key,
        "turns": turns,
        "limit": limit,
        "available_sessions": available,
    }


def _build_turn_detail_context(
    request: Request, *, session_key: str, turn_id: str
) -> dict[str, Any] | None:
    """Assemble the per-turn detail context. Returns ``None``
    when the capture isn't found — the route handler turns that
    into a 404."""
    import json as _json

    store = getattr(request.app.state, "turn_capture", None)
    if store is None:
        return None
    cap = store.read(session_key, turn_id)
    if cap is None:
        return None

    started = cap.started_at
    finished = cap.finished_at
    latency_ms = int(max(0.0, (finished - started) * 1000))

    messages: list[dict[str, Any]] = []
    for m in cap.dispatched_messages:
        if not isinstance(m, dict):
            continue
        messages.append(
            {
                "role": m.get("role", "?"),
                "content_preview": _format_message_preview(m.get("content")),
                "content_full": _format_message_full(m.get("content")),
            }
        )

    tool_calls: list[dict[str, Any]] = []
    for tc in cap.tool_calls:
        tool_calls.append(
            {
                "iteration": tc.iteration,
                "tool_name": tc.tool_name,
                "decision": tc.decision,
                "decision_detail": tc.decision_detail,
                "duration_ms": tc.duration_ms,
                "ok": tc.ok,
                "args_json": _json.dumps(tc.args, indent=2, ensure_ascii=False),
                "result_summary": tc.result_summary,
                "artifact_path": tc.artifact_path,
            }
        )

    day = datetime.fromtimestamp(finished, tz=UTC).strftime("%Y-%m-%d")

    turn = {
        "turn_id": cap.turn_id,
        "alias": cap.alias,
        "client": cap.client,
        "model_used": cap.model_used,
        "backend": cap.backend,
        "fallback_used": cap.fallback_used,
        "started_iso": _fmt_iso(started),
        "finished_iso": _fmt_iso(finished),
        "prompt_human": _fmt_tokens(cap.prompt_tokens),
        "completion_human": _fmt_tokens(cap.completion_tokens),
        "context_window_human": (_fmt_tokens(cap.context_window) if cap.context_window else "?"),
        "fill_human": (
            f"{cap.prompt_pct_of_window:.0f}%" if cap.prompt_pct_of_window is not None else "—"
        ),
        "latency_human": (f"{latency_ms / 1000:.2f}s" if latency_ms >= 1000 else f"{latency_ms}ms"),
        "iterations": cap.iterations,
        "tool_calls_count": len(cap.tool_calls),
        "finish_reason": cap.finish_reason,
        "narration_warning": cap.narration_warning,
        "status": cap.status,
        "dispatched_messages": messages,
        "tool_calls": tool_calls,
        "response_pretty": _json.dumps(cap.response, indent=2, ensure_ascii=False),
        "response_text": _flatten_response_text(cap.response),
        "day": day,
    }
    return {"session_key": session_key, "turn": turn}


def _render_turn_list(request: Request, *, session_key: str | None, client: str) -> Response:
    key = _safe_session_key(session_key or "main")
    ctx = _build_turn_list_context(request, session_key=key)
    ctx["client"] = client
    return templates.TemplateResponse(request, "turns_list.html", ctx)


def _render_turn_detail(
    request: Request, *, session_key: str, turn_id: str, client: str
) -> Response:
    key = _safe_session_key(session_key)
    # Don't sanitise turn_id — it's a UUID; characters outside
    # [0-9a-f-] would fail the file lookup naturally.
    ctx = _build_turn_detail_context(request, session_key=key, turn_id=turn_id)
    if ctx is None:

        body = templates.TemplateResponse(
            request,
            "turn_not_found.html",
            {"client": client, "session_key": key, "turn_id": turn_id},
            status_code=404,
        )
        return body
    ctx["client"] = client
    return templates.TemplateResponse(request, "turns_detail.html", ctx)


# --------------------------------------------------------------- placeholder helper


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

    # --------------------------------------------------------- aliases

    @router.get("/aliases", response_class=HTMLResponse)
    async def aliases_view(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_aliases_context(request)
        ctx["client"] = getattr(request.state, "client", "webui")
        return templates.TemplateResponse(request, "aliases.html", ctx)

    @router.get("/_partials/aliases", response_class=HTMLResponse)
    async def aliases_partial(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_aliases_context(request)
        return templates.TemplateResponse(request, "_aliases_panel.html", ctx)

    # --------------------------------------------------------- turns
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
        client = getattr(request.state, "client", "webui")
        if turn_id is not None and session is not None:
            return _render_turn_detail(
                request,
                session_key=session,
                turn_id=turn_id,
                client=client,
            )
        return _render_turn_list(
            request,
            session_key=session,
            client=client,
        )

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
