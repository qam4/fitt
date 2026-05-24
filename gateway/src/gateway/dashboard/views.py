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
from decimal import Decimal
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


# --------------------------------------------------------------- settings + projects + identity + skills + sessions + cost (read-only)


def _redact_secret_value(value: Any) -> str:
    """Render a secret as 'configured' or 'not configured' — never
    the raw bytes. Even the redacted view of secrets.yaml never
    leaks a token, an api key, or a bot token through the
    dashboard's response surface. Pure presence flag."""
    if value is None or value == "":
        return "not configured"
    return "configured"


def _build_settings_context(request: Request) -> dict[str, Any]:
    """Render the loaded ``Config`` object plus a redacted view
    of ``secrets.yaml``. Read-only.

    The settings view is the operator's "what does FITT think
    its config is" pane. Useful when something behaves
    unexpectedly and you want to confirm whether a recent edit
    landed before sshing in to read the file.

    The redaction floor: token names + their ``client:`` tag,
    presence of provider keys ("configured" / "not
    configured"), allowlist user IDs from telegram. **No raw
    values rendered, ever** — even though we're read-only,
    future-author opening the dashboard with a screen-share
    running shouldn't accidentally leak the OpenRouter key.
    """
    config = request.app.state.config
    secrets = getattr(config, "secrets", None)

    # Models list. We keep cost rates; they're not secrets, but
    # they're load-bearing for the cost view.
    models: list[dict[str, Any]] = []
    for m in config.models:
        models.append(
            {
                "id": m.id,
                "backend": m.backend,
                "model": m.model,
                "endpoint": m.endpoint or "",
                "fallback": m.fallback or "",
                "cost_in": str(m.cost_per_mtok_in),
                "cost_out": str(m.cost_per_mtok_out),
            }
        )

    # MCP server config (raw dicts; the manager describes the
    # running state on /dashboard/health).
    mcp_servers: list[dict[str, Any]] = []
    for raw in config.mcp_servers or []:
        if isinstance(raw, dict):
            mcp_servers.append(
                {
                    "name": str(raw.get("name", "?")),
                    "command": str(raw.get("command", "?")),
                    "args": list(raw.get("args") or []),
                }
            )

    # Redacted secrets snapshot.
    secrets_view: dict[str, Any] = {}
    if secrets is None:
        secrets_view = {
            "loaded": False,
            "tokens": [],
            "providers": [],
            "telegram_configured": False,
            "telegram_allowlist": [],
        }
    else:
        token_rows: list[dict[str, Any]] = []
        for t in secrets.allowed_tokens:
            token_rows.append(
                {
                    "name": t.name,
                    "client": t.client or "(untagged)",
                }
            )
        provider_rows: list[dict[str, Any]] = [
            {
                "name": "openrouter",
                "status": _redact_secret_value(secrets.openrouter_api_key),
            },
            {
                "name": "anthropic",
                "status": _redact_secret_value(secrets.anthropic_api_key),
            },
        ]
        for model_id, key in (secrets.api_keys or {}).items():
            provider_rows.append(
                {
                    "name": f"api_keys.{model_id}",
                    "status": _redact_secret_value(key),
                }
            )
        secrets_view = {
            "loaded": True,
            "tokens": token_rows,
            "providers": provider_rows,
            "telegram_configured": secrets.telegram is not None,
            "telegram_allowlist": (
                list(secrets.telegram.allowlist_user_ids) if secrets.telegram is not None else []
            ),
        }

    # SSH public key — useful as a copy button for satellite
    # setup. Private key is never read; only the .pub file.
    ssh_pubkey: str | None = None
    ssh_key_path = getattr(request.app.state, "ssh_key_path", None)
    if ssh_key_path is not None:
        pub_path = Path(str(ssh_key_path) + ".pub")
        if pub_path.exists():
            try:
                ssh_pubkey = pub_path.read_text(encoding="utf-8").strip()
            except OSError:
                ssh_pubkey = None

    # Run the boot validators a second time so the operator
    # sees what they'd see on a restart.
    from ..config import check_missing_api_keys

    config_warnings = check_missing_api_keys(config)

    return {
        "aliases": config.aliases,
        "models": models,
        "memory": {
            "enabled": config.memory.enabled,
            "max_history_chars": config.memory.max_history_chars,
            "max_lessons": config.memory.max_lessons,
            "history_max_days": config.memory.history_max_days,
            "skills_enabled": config.memory.skills_enabled,
            "identity_dir": str(config.memory.identity_dir),
            "sessions_dir": str(config.memory.sessions_dir),
            "skills_dir": str(config.memory.skills_dir),
        },
        "traceability": {
            "enabled": config.traceability.enabled,
            "default_capture": list(config.traceability.default_capture),
        },
        "web": {"search_backend": config.web.search_backend},
        "server": {
            "host": config.server.host,
            "port": config.server.port,
            "log_level": config.server.log_level,
            "log_bodies": config.server.log_bodies,
            "boot_probe_enabled": config.server.boot_probe_enabled,
            "context_probe_timeout_s": config.server.context_probe_timeout_s,
        },
        "upstream_timeout_secs": config.upstream_timeout_secs,
        "mcp_servers": mcp_servers,
        "tools_block_present": config.tools is not None,
        "events_block_present": config.events is not None,
        "secrets_view": secrets_view,
        "ssh_pubkey": ssh_pubkey,
        "config_warnings": config_warnings,
    }


def _build_projects_context(request: Request) -> dict[str, Any]:
    registry = getattr(request.app.state, "project_registry", None)
    rows: list[dict[str, Any]] = []
    if registry is not None:
        try:
            projects = registry.all()
        except Exception:
            projects = []
        for p in projects:
            rows.append(
                {
                    "name": p.name,
                    "path": p.path,
                    "ssh_host": p.ssh_host or "(local)",
                    "is_local": p.is_local,
                    "test_command": p.test_command,
                    "build_command": p.build_command,
                }
            )
    return {"project_rows": rows}


def _build_identity_context(request: Request) -> dict[str, Any]:
    """Read the identity files + lessons.md and return a
    rendered + raw view for each.

    Files: ``user.md``, ``soul.md``, ``tools.md`` per
    :class:`MemoryStore._load_identity` plus ``lessons.md``.
    Missing files render empty-state cells; the dashboard
    doesn't try to seed them (memory.py owns that)."""
    config = request.app.state.config
    identity_dir = Path(config.memory.identity_dir)

    files: list[dict[str, Any]] = []
    for name in ("user.md", "soul.md", "tools.md", "lessons.md"):
        path = identity_dir / name
        if not path.exists():
            files.append({"name": name, "exists": False, "raw": "", "rendered": ""})
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            files.append(
                {
                    "name": name,
                    "exists": True,
                    "raw": "",
                    "rendered": f"<em>read failed: {type(exc).__name__}</em>",
                    "error": str(exc),
                }
            )
            continue
        files.append(
            {
                "name": name,
                "exists": True,
                "raw": raw,
                "rendered": _render_markdown(raw),
                "mtime": path.stat().st_mtime,
                "size": path.stat().st_size,
            }
        )

    # Also surface the lessons store's parsed view so the
    # operator can see how the file is interpreted.
    lessons_store = getattr(request.app.state, "lessons", None)
    parsed_lessons: list[dict[str, Any]] = []
    if lessons_store is not None:
        try:
            for lesson in lessons_store.read():
                parsed_lessons.append(
                    {
                        "category": lesson.category or "",
                        "text": lesson.text,
                    }
                )
        except Exception:
            pass

    return {
        "identity_dir": str(identity_dir),
        "files": files,
        "parsed_lessons": parsed_lessons,
    }


def _render_markdown(text: str) -> str:
    """Cheap CommonMark → HTML rendering for the dashboard's
    markdown views. Reuses ``markdown_it`` (already vendored
    via the telegram-bot but not yet in the gateway). Falls
    back to ``<pre>`` escaping when markdown_it isn't
    available so a missing dep doesn't break the view."""
    try:
        from markdown_it import MarkdownIt
    except ImportError:  # pragma: no cover - fallback
        import html as _html

        return f"<pre>{_html.escape(text)}</pre>"
    md = MarkdownIt("commonmark")
    rendered: str = md.render(text)
    return rendered


def _build_skills_context(request: Request) -> dict[str, Any]:
    skills = getattr(request.app.state, "skills", None) or []
    rows: list[dict[str, Any]] = []
    for s in skills:
        path = s.skill_md_path
        try:
            mtime = path.stat().st_mtime if path.exists() else None
        except OSError:
            mtime = None
        rows.append(
            {
                "name": s.name,
                "description": s.description,
                "description_truncated": s.description_truncated,
                "prerequisites": list(s.prerequisites),
                "path": str(path),
                "mtime_human": _fmt_age(mtime) if mtime else "—",
            }
        )
    config = request.app.state.config
    return {
        "skills_dir": str(config.memory.skills_dir),
        "skills_enabled": config.memory.skills_enabled,
        "skill_rows": rows,
    }


def _build_sessions_context(request: Request) -> dict[str, Any]:
    """List known sessions from the registry plus the per-
    session day-count of history files. Same data
    ``fitt session list`` shows, plus a hint about how much
    history each session has."""
    config = request.app.state.config
    registry = getattr(request.app.state, "session_registry", None)
    sessions_dir = Path(config.memory.sessions_dir)

    rows: list[dict[str, Any]] = []
    if registry is not None:
        try:
            sessions = registry.all(include_archived=True)
        except Exception:
            sessions = []
        for s in sessions:
            history_dir = sessions_dir / s.id / "history"
            day_count = 0
            try:
                if history_dir.exists():
                    day_count = sum(1 for _ in history_dir.glob("*.md"))
            except OSError:
                day_count = 0
            captures_dir = sessions_dir / s.id / "turns"
            capture_day_count = 0
            try:
                if captures_dir.exists():
                    capture_day_count = sum(1 for p in captures_dir.iterdir() if p.is_dir())
            except OSError:
                capture_day_count = 0
            rows.append(
                {
                    "id": s.id,
                    "name": s.name,
                    "archived": s.archived,
                    "created_iso": s.created_at.isoformat(),
                    "history_days": day_count,
                    "capture_days": capture_day_count,
                }
            )
    return {
        "sessions_dir": str(sessions_dir),
        "session_rows": rows,
    }


def _build_cost_context(request: Request, *, month_prefix: str | None) -> dict[str, Any]:
    """Aggregate monthly spend from ``gateway.log`` + rotated
    siblings via :func:`gateway.cost.aggregate_monthly_spend`.
    Same logic the ``fitt cost`` CLI uses; the dashboard view
    is a thin renderer over the dict it returns."""
    from ..cost import aggregate_monthly_spend

    config = request.app.state.config
    log_dir = Path(config.logging.dir)

    totals, prefix = aggregate_monthly_spend(log_dir, month_prefix=month_prefix)

    rows: list[dict[str, Any]] = []
    grand = Decimal("0")
    for model in sorted(totals.keys()):
        row = totals[model]
        grand += row["cost_usd"]
        rows.append(
            {
                "model": model,
                "requests": row["requests"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cost_usd": f"{row['cost_usd']:.4f}",
            }
        )
    return {
        "month_prefix": prefix,
        "log_dir": str(log_dir),
        "log_dir_exists": log_dir.exists(),
        "cost_rows": rows,
        "grand_total": f"{grand:.4f}",
    }


# --------------------------------------------------------------- tools / cron / audit / health / gaps


_BUCKET_CLASS = {
    "auto": "badge",
    "ask": "badge-warn",
    "block": "badge-error",
    "trust_session": "badge",
}


def _build_tools_context(request: Request) -> dict[str, Any]:
    """Tool registry + per-tool last invocation from the audit
    log. Audit-derived counts ride a 24h window."""
    app = request.app
    registry = getattr(app.state, "tool_registry", None)
    audit = getattr(app.state, "audit", None)
    now = time.time()

    # Build per-tool aggregates from the audit log. We walk the
    # log once and accumulate everything we need rather than
    # making one pass per tool.
    audit_entries: list[dict[str, Any]] = []
    if audit is not None:
        try:
            audit_entries = audit.iter_entries() or []
        except Exception:
            audit_entries = []

    cutoff = now - 86400
    counts_24h: dict[str, int] = {}
    last_invocation: dict[str, dict[str, Any]] = {}
    for entry in audit_entries:
        tool = entry.get("tool")
        if not isinstance(tool, str):
            continue
        try:
            ts = float(entry.get("ts", 0.0))
        except (TypeError, ValueError):
            continue
        if ts >= cutoff:
            counts_24h[tool] = counts_24h.get(tool, 0) + 1
        prev = last_invocation.get(tool)
        if prev is None or ts > prev.get("ts", 0.0):
            last_invocation[tool] = {
                "ts": ts,
                "decision": entry.get("decision", "?"),
                "ok": bool(entry.get("ok", False)),
            }

    tool_rows: list[dict[str, Any]] = []
    if registry is not None:
        for t in registry.list_all():
            bucket = t.default_bucket.value
            last = last_invocation.get(t.name)
            tool_rows.append(
                {
                    "name": t.name,
                    "kind": t.kind,
                    "description": t.description,
                    "bucket": bucket,
                    "bucket_class": _BUCKET_CLASS.get(bucket, "badge-dim"),
                    "requires_project": t.requires_project,
                    "calls_24h": counts_24h.get(t.name, 0),
                    "last_invocation_age": _fmt_age(last["ts"]) if last else "never",
                    "last_decision": last["decision"] if last else "—",
                }
            )
    return {
        "tool_rows": tool_rows,
        "generated_at_human": datetime.fromtimestamp(now, tz=UTC).strftime("%H:%M:%S UTC"),
    }


def _format_schedule(sched: Any) -> str:
    """Compact human form for a CronSchedule. Mirrors the CLI's
    rendering so the dashboard reads consistent with `fitt cron list`."""
    if sched is None:
        return "—"
    kind = getattr(sched, "kind", "?")
    if kind == "every":
        secs = getattr(sched, "every_secs", None)
        if not secs:
            return "every ?"
        return f"every {_fmt_duration(float(secs))}"
    if kind == "at":
        ts = getattr(sched, "at_ts", None)
        if ts is None:
            return "at (consumed)"
        return f"at {_fmt_iso(ts)}"
    if kind == "cron":
        expr = getattr(sched, "cron_expr", "?") or "?"
        tz = getattr(sched, "timezone", "UTC")
        return f"{expr} ({tz})"
    return str(kind)


def _build_cron_context(request: Request) -> dict[str, Any]:
    cron_service = getattr(request.app.state, "cron", None)
    now = time.time()
    cron_rows: list[dict[str, Any]] = []
    if cron_service is not None:
        try:
            jobs = cron_service.list(include_disabled=True)
        except Exception:
            jobs = []
        for j in jobs:
            schedule = getattr(j, "schedule", None)
            try:
                next_ts = schedule.next_run(after=now) if schedule else None
            except Exception:
                next_ts = None
            message = getattr(j, "message", "") or ""
            preview = (message[:90] + "…") if len(message) > 90 else (message or "(empty)")
            cron_rows.append(
                {
                    "id": j.id,
                    "name": j.name,
                    "schedule_human": _format_schedule(schedule),
                    "next_firing_human": _fmt_iso(next_ts) if next_ts else "—",
                    "last_run_age": _fmt_age(j.last_run_ts),
                    "last_status": j.last_status,
                    "enabled": j.enabled,
                    "silent": j.silent,
                    "created_by_client": j.created_by_client,
                    "message": message,
                    "message_preview": preview,
                }
            )
    return {
        "cron_rows": cron_rows,
        "generated_at_human": datetime.fromtimestamp(now, tz=UTC).strftime("%H:%M:%S UTC"),
    }


def _build_audit_context(
    request: Request, *, tool_filter: str | None, limit: int
) -> dict[str, Any]:
    audit = getattr(request.app.state, "audit", None)
    audit_entries: list[dict[str, Any]] = []
    if audit is not None:
        try:
            audit_entries = audit.iter_entries() or []
        except Exception:
            audit_entries = []
    if tool_filter:
        audit_entries = [e for e in audit_entries if e.get("tool") == tool_filter]
    # Newest first.
    audit_entries = audit_entries[-limit:][::-1]

    rows: list[dict[str, Any]] = []
    for e in audit_entries:
        try:
            ts = float(e.get("ts", 0.0))
        except (TypeError, ValueError):
            ts = 0.0
        rows.append(
            {
                "ts_iso": _fmt_iso(ts),
                "age_human": _fmt_age(ts),
                "tool": e.get("tool", "?"),
                "client": e.get("client", "?"),
                "decision": e.get("decision", "?"),
                "session_key": e.get("session_key", "?"),
                "duration_ms": e.get("duration_ms", 0),
                "ok": bool(e.get("ok", False)),
                "error": (e.get("error") or "").strip(),
            }
        )
    return {
        "audit_rows": rows,
        "tool_filter": tool_filter,
        "limit": limit,
    }


def _build_health_context(request: Request) -> dict[str, Any]:
    """Mirrors the /v1/status payload, plus the alias summary
    from the overview view."""
    app = request.app
    home = _fitt_home()
    now = time.time()

    started_at: float | None = getattr(app.state, "started_at", None)
    if started_at is None:
        started_at = now
    uptime_s = max(0.0, now - started_at)

    mcp_servers: list[dict[str, Any]] = []
    mcp_manager = getattr(app.state, "mcp", None)
    if mcp_manager is not None:
        try:
            mcp_servers = list(mcp_manager.describe() or [])
        except Exception:
            mcp_servers = []
    mcp_running = sum(1 for s in mcp_servers if s.get("running"))
    mcp_total = len(mcp_servers)

    cron_total = 0
    cron_enabled = 0
    cron_next_firing_ts: float | None = None
    cron_service = getattr(app.state, "cron", None)
    if cron_service is not None:
        try:
            jobs = cron_service.list(include_disabled=True)
            cron_total = len(jobs)
            soonest: float | None = None
            for j in jobs:
                if not getattr(j, "enabled", True):
                    continue
                cron_enabled += 1
                schedule = getattr(j, "schedule", None)
                try:
                    nxt = schedule.next_run(after=now) if schedule else None
                except Exception:
                    nxt = None
                if nxt is not None and (soonest is None or nxt < soonest):
                    soonest = float(nxt)
            cron_next_firing_ts = soonest
        except Exception:
            pass

    gap_count = 0
    gap_log = getattr(app.state, "capability_gaps", None)
    if gap_log is not None:
        try:
            gap_count = len(gap_log.read())
        except Exception:
            pass

    history_last_sweep = _read_anchor_ts(home / "history.pruner.anchor")
    event_last_sweep = _read_anchor_ts(home / "events.pruner.anchor")

    config = app.state.config
    secrets = getattr(config, "secrets", None)
    telegram_configured = bool(secrets and getattr(secrets, "telegram", None))

    # Alias counts mirror the overview's logic.
    probe_results: dict[str, Any] = getattr(app.state, "alias_probe_results", {}) or {}
    alias_count = len(config.alias_names())
    alias_ok_count = sum(1 for p in probe_results.values() if getattr(p, "status", "") == "ok")

    return {
        "uptime_human": _fmt_duration(uptime_s),
        "started_at_human": _fmt_iso(started_at),
        "mcp_running": mcp_running,
        "mcp_total": mcp_total,
        "mcp_servers": mcp_servers,
        "cron_total": cron_total,
        "cron_enabled": cron_enabled,
        "cron_next_firing_human": _fmt_iso(cron_next_firing_ts) if cron_next_firing_ts else "",
        "gap_count": gap_count,
        "history_pruner_text": _fmt_age(history_last_sweep),
        "event_pruner_text": _fmt_age(event_last_sweep),
        "telegram_text": "configured" if telegram_configured else "not configured",
        "alias_count": alias_count,
        "alias_ok_count": alias_ok_count,
        "generated_at_human": datetime.fromtimestamp(now, tz=UTC).strftime("%H:%M:%S UTC"),
    }


def _build_gaps_context(request: Request) -> dict[str, Any]:
    gap_log = getattr(request.app.state, "capability_gaps", None)
    rows: list[dict[str, Any]] = []
    if gap_log is not None:
        try:
            from ..capabilities import rank_gaps as _rank_gaps

            ranked = _rank_gaps(gap_log.read())
        except Exception:
            ranked = []
        for action, count, gap in ranked:
            rows.append(
                {
                    "action": action,
                    "count": count,
                    "last_suggestion": gap.suggestion,
                    "last_seen_human": _fmt_age(gap.ts),
                }
            )
    return {"gap_rows": rows}


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
        ctx = _build_tools_context(request)
        ctx["client"] = getattr(request.state, "client", "webui")
        return templates.TemplateResponse(request, "tools.html", ctx)

    @router.get("/cron", response_class=HTMLResponse)
    async def cron_view(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_cron_context(request)
        ctx["client"] = getattr(request.state, "client", "webui")
        return templates.TemplateResponse(request, "cron.html", ctx)

    @router.get("/audit", response_class=HTMLResponse)
    async def audit_view(
        request: Request,
        tool: str | None = None,
        limit: int = 100,
    ) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        # Clamp limit so a huge ?limit=99999 query doesn't hang
        # the renderer; 1000 covers the worst-case forensic
        # browse.
        clamped = max(1, min(limit, 1000))
        ctx = _build_audit_context(request, tool_filter=tool, limit=clamped)
        ctx["client"] = getattr(request.state, "client", "webui")
        return templates.TemplateResponse(request, "audit.html", ctx)

    @router.get("/health", response_class=HTMLResponse)
    async def health_view(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_health_context(request)
        ctx["client"] = getattr(request.state, "client", "webui")
        return templates.TemplateResponse(request, "health.html", ctx)

    @router.get("/_partials/health", response_class=HTMLResponse)
    async def health_partial(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_health_context(request)
        return templates.TemplateResponse(request, "_health_panel.html", ctx)

    @router.get("/gaps", response_class=HTMLResponse)
    async def gaps_view(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_gaps_context(request)
        ctx["client"] = getattr(request.state, "client", "webui")
        return templates.TemplateResponse(request, "gaps.html", ctx)

    # --------------------------------------------------------- introspection (F9)

    @router.get("/settings", response_class=HTMLResponse)
    async def settings_view(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_settings_context(request)
        ctx["client"] = getattr(request.state, "client", "webui")
        return templates.TemplateResponse(request, "settings.html", ctx)

    @router.get("/projects", response_class=HTMLResponse)
    async def projects_view(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_projects_context(request)
        ctx["client"] = getattr(request.state, "client", "webui")
        return templates.TemplateResponse(request, "projects.html", ctx)

    @router.get("/identity", response_class=HTMLResponse)
    async def identity_view(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_identity_context(request)
        ctx["client"] = getattr(request.state, "client", "webui")
        return templates.TemplateResponse(request, "identity.html", ctx)

    @router.get("/skills", response_class=HTMLResponse)
    async def skills_view(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_skills_context(request)
        ctx["client"] = getattr(request.state, "client", "webui")
        return templates.TemplateResponse(request, "skills.html", ctx)

    @router.get("/sessions", response_class=HTMLResponse)
    async def sessions_view(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_sessions_context(request)
        ctx["client"] = getattr(request.state, "client", "webui")
        return templates.TemplateResponse(request, "sessions.html", ctx)

    @router.get("/cost", response_class=HTMLResponse)
    async def cost_view(
        request: Request,
        month: str | None = None,
    ) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_cost_context(request, month_prefix=month)
        ctx["client"] = getattr(request.state, "client", "webui")
        return templates.TemplateResponse(request, "cost.html", ctx)

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
