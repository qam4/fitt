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

import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
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


def _fmt_iso(ts: float | None, *, mode: str = "full") -> str:
    """Render a UNIX timestamp as a display label in the hub's
    local timezone.

    ``astimezone()`` with no argument converts to whatever zone
    the process is in — the container's ``TZ`` env (set in
    docker-compose), or the host zone for a native run. This is
    the same clock the ``[Current time]`` capability block and
    the cron scheduler reason in, so the dashboard, the model,
    and the scheduler all agree.

    ``mode``: ``"full"`` → ``YYYY-MM-DD HH:MM:SS <TZ>``; ``"time"``
    → ``HH:MM:SS <TZ>`` (for the "refreshed at" footers where the
    date is implied).

    Only display *labels* flow through here. Data the operator
    inspects (raw response JSON, dispatched messages, audit/eval
    content) is rendered verbatim elsewhere and stays in whatever
    form it was stored — never localized — so a log line always
    shows the exact value it holds.
    """
    if ts is None:
        return "—"
    dt = datetime.fromtimestamp(ts, tz=UTC).astimezone()
    tz_name = dt.tzname() or "local"
    if mode == "time":
        return f"{dt.strftime('%H:%M:%S')} {tz_name}"
    return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} {tz_name}"


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
        "generated_at_human": _fmt_iso(now, mode="time"),
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


def _read_eval_json(md_path: Path) -> dict[str, Any] | None:
    """Read the structured JSON sidecar (``<...>-latest.json``) that
    :func:`gateway.alias_eval.write_report` writes beside the markdown.

    Returns the dashboard-shaped dict (mapping the report's
    ``finished_at`` to the template's ``finished_iso``), or ``None`` when
    the sidecar is absent or unparseable - so the caller falls back to
    regex-parsing the markdown (legacy reports written before the JSON
    sidecar existed). The structured read replaces the brittle
    render-to-markdown-then-parse-it-back round-trip."""
    json_path = md_path.with_suffix(".json")
    if not json_path.exists():
        return None
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "passed" not in data or "total" not in data:
        return None
    total = data["total"]
    cases = data.get("cases")
    return {
        "passed": data["passed"],
        "total": total,
        "pass_rate": data.get("pass_rate", (data["passed"] / total if total else 0.0)),
        "finished_iso": data.get("finished_at"),
        "model_id": data.get("model_id"),
        "duration_ms": data.get("duration_ms"),
        "cases": cases if isinstance(cases, list) else [],
    }


def _parse_eval_report(path: Path) -> dict[str, Any] | None:
    """Read the rolling per-alias eval report header. Returns the
    summary dict or ``None`` when the file's missing / unparseable.
    Same parser the ``/v1/aliases`` endpoint uses; duplicated
    locally to avoid the cross-module import for the dashboard's
    hot path.

    Prefers the structured JSON sidecar; falls back to parsing the
    markdown header for legacy reports written before it."""
    j = _read_eval_json(path)
    if j is not None:
        return {
            "passed": j["passed"],
            "total": j["total"],
            "pass_rate": j["pass_rate"],
            "finished_iso": j["finished_iso"],
        }
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


def _format_eval_cell(alias: str, report: dict[str, Any] | None) -> str:
    """Render the last-eval column.

    Returns HTML so we can include a colour-coded pass-rate
    badge wrapped in a link to the per-alias eval detail view.
    Caller marks the value safe in the template.

    Pre-F18 this just rendered the badge; F18 wraps it in a
    link to the per-alias detail view so the operator can drill
    into per-case detail without dropping to the CLI. Phase 7.6:
    that detail now lives on the unified ``/dashboard/alias/<id>``
    page (the eval section), so the badge links there. The "—"
    placeholder for missing reports stays unlinked because
    there's nothing to drill into."""
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
    badge = f'<span class="{cls}">{passed}/{total} ({pct}%)</span>'
    return f'<a href="/dashboard/alias/{alias}#eval" class="bare">{badge}</a>'


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


# --------------------------------------------------------------- F18: eval detail


# Parsing the per-case sections of the markdown report. Mirrors
# :func:`gateway.alias_eval.render_report_markdown`'s output:
#
#   ### ✅ `case_name` — pass
#   ### ❌ `case_name` — narrated
#
# Each case section has a status, latency, optional tool_called
# and finish_reason, a free-form detail line, and an optional
# reply preview inside a fenced block.
_EVAL_CASE_HEADER_RE = re.compile(r"^###\s+(?:✅|❌)\s+`(?P<name>[^`]+)`\s+—\s+(?P<status>\S+)\s*$")
_EVAL_CASE_LATENCY_RE = re.compile(r"^-\s+Latency:\s+(?P<ms>\d+)\s*ms")
_EVAL_CASE_TOOL_CALLED_RE = re.compile(r"^-\s+Tool called:\s+`(?P<name>[^`]+)`")
_EVAL_CASE_FINISH_RE = re.compile(r"^-\s+Finish reason:\s+`(?P<reason>[^`]+)`")
_EVAL_CASE_DETAIL_RE = re.compile(r"^-\s+Detail:\s+(?P<detail>.+)$")
_EVAL_CASE_MODEL_RE = re.compile(r"^-\s+Model:\s+`(?P<model>[^`]+)`")
_EVAL_CASE_DURATION_RE = re.compile(r"^-\s+Duration:\s+(?P<ms>\d+)\s*ms")


def _parse_eval_report_full(path: Path) -> dict[str, Any] | None:
    """Parse the full per-alias eval markdown report.

    Returns a dict with the header summary plus a ``cases`` list
    where each entry mirrors :class:`gateway.alias_eval.CaseResult`'s
    user-visible fields. Returns ``None`` when the file's missing
    or so malformed we couldn't even pull the result line.

    The parser deliberately tolerates extra blank lines, missing
    optional fields (``tool_called`` / ``finish_reason`` /
    ``reply_preview``), and shrugs at unknown bullet keys —
    same forgiving posture as :func:`_parse_eval_report`. If a
    section header lands without any subsequent fields parsing
    cleanly, the case is still emitted with whatever did parse
    so the operator gets partial detail rather than a 500.

    Prefers the structured JSON sidecar; falls back to parsing the
    markdown for legacy reports written before it."""
    j = _read_eval_json(path)
    if j is not None:
        return j
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    passed: int | None = None
    total: int | None = None
    finished_iso: str | None = None
    model_id: str | None = None
    duration_ms: int | None = None
    cases: list[dict[str, Any]] = []

    cur: dict[str, Any] | None = None
    in_preview = False
    preview_lines: list[str] = []

    for raw in text.splitlines():
        line = raw.rstrip("\r")

        # Reply preview blocks are fenced with ``` indented
        # under the bullet list. Track open/close.
        stripped = line.strip()
        if cur is not None and stripped == "```":
            if in_preview:
                cur["reply_preview"] = "\n".join(preview_lines).strip()
                preview_lines = []
                in_preview = False
            else:
                in_preview = True
            continue
        if in_preview:
            # Strip the two-space indent render_report_markdown
            # adds to keep the preview inside the bullet.
            preview_lines.append(line[2:] if line.startswith("  ") else line)
            continue

        # Header lines (only fire before any case section).
        if not cases and cur is None:
            m = _EVAL_RESULT_RE.match(line)
            if m is not None:
                passed = int(m.group("passed"))
                total = int(m.group("total"))
                continue
            m = _EVAL_FINISHED_RE.match(line)
            if m is not None:
                finished_iso = m.group("iso")
                continue
            m = _EVAL_CASE_MODEL_RE.match(line)
            if m is not None:
                model_id = m.group("model")
                continue
            m = _EVAL_CASE_DURATION_RE.match(line)
            if m is not None:
                duration_ms = int(m.group("ms"))
                continue

        m = _EVAL_CASE_HEADER_RE.match(line)
        if m is not None:
            if cur is not None:
                cases.append(cur)
            cur = {
                "name": m.group("name"),
                "status": m.group("status"),
                "latency_ms": None,
                "tool_called": None,
                "finish_reason": None,
                "detail": "",
                "reply_preview": "",
            }
            continue

        if cur is None:
            continue

        m = _EVAL_CASE_LATENCY_RE.match(line)
        if m is not None:
            cur["latency_ms"] = int(m.group("ms"))
            continue
        m = _EVAL_CASE_TOOL_CALLED_RE.match(line)
        if m is not None:
            cur["tool_called"] = m.group("name")
            continue
        m = _EVAL_CASE_FINISH_RE.match(line)
        if m is not None:
            cur["finish_reason"] = m.group("reason")
            continue
        m = _EVAL_CASE_DETAIL_RE.match(line)
        if m is not None:
            cur["detail"] = m.group("detail").strip()
            continue

    # Flush trailing case (the last one has no following header).
    if cur is not None:
        cases.append(cur)

    if passed is None or total is None:
        return None

    return {
        "passed": passed,
        "total": total,
        "pass_rate": passed / total if total > 0 else 0.0,
        "finished_iso": finished_iso,
        "model_id": model_id,
        "duration_ms": duration_ms,
        "cases": cases,
    }


# Verdict tone classes for the badge / banner.
_VERDICT_RECOMMENDED = "recommended"
_VERDICT_WORKABLE = "workable"
_VERDICT_RISKY = "risky"
_VERDICT_NOT_RECOMMENDED = "not_recommended"
_VERDICT_INCOMPLETE = "incomplete"

# Eval case statuses that mean "the dispatch didn't complete
# cleanly" — the model never got a fair chance, so no verdict
# can be drawn. Phase 7.6 replaced the single ``transport_error``
# with the shared dispatch-outcome taxonomy plus ``empty_reply``.
# Mirrors ``alias_eval.DISPATCH_FAILURE_STATUSES`` (kept local to
# avoid the views layer importing the eval runner just for a
# constant).
_EVAL_DISPATCH_FAILURE_STATUSES: frozenset[str] = frozenset(
    {
        "upstream_silent",
        "unreachable",
        "upstream_rate_limited",
        "upstream_client_error",
        "upstream_server_error",
        "empty_reply",
    }
)


def _eval_verdict(parsed: dict[str, Any] | None) -> dict[str, str]:
    """Map a parsed eval report to a verdict bucket + reason.

    The pass-rate alone doesn't tell the operator whether a
    binding is safe — ``4/5`` with the ``no_tool_small_talk``
    case failing is workable; ``4/5`` with one of the tool-
    required cases narrating is the granite-incident shape
    and should be treated as risky.

    Returns ``{"label", "tone", "reason"}`` where ``tone`` is
    one of the ``_VERDICT_*`` constants for CSS class mapping.
    Sharp version: one short reason per bucket; the per-case
    detail in the page below carries the explanation if the
    operator wants to dig.

    Buckets:

    * ``recommended`` — every case passed.
    * ``workable`` — only the ``no_tool_expected_but_called``
      case failed (over-eager but tool-calling discipline
      otherwise intact). Or a single ``wrong_tool`` on the
      disambiguation case, which is shaky but salvageable.
    * ``risky`` — any tool-required case failed with
      ``narrated`` (granite shape) or ``truncated``. The
      binding will silently fail real Telegram tool turns.
    * ``not_recommended`` — multiple failures, or pass rate
      below 60%.
    * ``incomplete`` — at least one dispatch-failure status
      (``upstream_silent`` / ``unreachable`` /
      ``upstream_rate_limited`` / ``upstream_client_error`` /
      ``upstream_server_error`` / ``empty_reply``); can't make a
      verdict until the model is reachable, warm, and the suite
      re-runs cleanly.
    """
    if parsed is None or not parsed.get("cases"):
        return {
            "label": "No eval yet",
            "tone": _VERDICT_INCOMPLETE,
            "reason": "Run the eval suite from the Aliases tab to see a verdict.",
        }

    cases: list[dict[str, Any]] = parsed["cases"]
    statuses = [c.get("status", "") for c in cases]
    failed = [s for s in statuses if s != "pass"]
    dispatch_failures = [s for s in statuses if s in _EVAL_DISPATCH_FAILURE_STATUSES]
    narrated = [s for s in statuses if s == "narrated"]
    truncated = [s for s in statuses if s == "truncated"]
    over_eager = [s for s in statuses if s == "no_tool_expected_but_called"]
    wrong_tool = [s for s in statuses if s == "wrong_tool"]

    if dispatch_failures:
        return {
            "label": "Incomplete",
            "tone": _VERDICT_INCOMPLETE,
            "reason": (
                f"{len(dispatch_failures)} case(s) failed to dispatch "
                "cleanly (slow/cold-loading, unreachable, rate-limited, or "
                "empty reply). The model wasn't given a fair chance — "
                "re-test after the model is warm and reachable."
            ),
        }

    if not failed:
        return {
            "label": "Recommended",
            "tone": _VERDICT_RECOMMENDED,
            "reason": "All five tool-call patterns work. Safe to bind.",
        }

    if narrated:
        return {
            "label": "Risky",
            "tone": _VERDICT_RISKY,
            "reason": (
                f"{len(narrated)} case(s) returned narrated text instead of "
                "real tool_calls. The granite-incident shape; expect "
                "failures on real tool-use turns under FITT's full prompt."
            ),
        }

    if truncated:
        return {
            "label": "Risky",
            "tone": _VERDICT_RISKY,
            "reason": (
                f"{len(truncated)} case(s) hit the max_tokens cap before "
                "emitting tool_calls. Raise max_tokens or rebind."
            ),
        }

    # Pure-overeager: tool_required cases passed, only the
    # negative case failed. Workable.
    if len(failed) == 1 and over_eager:
        return {
            "label": "Workable",
            "tone": _VERDICT_WORKABLE,
            "reason": (
                "Tool-calling discipline is intact, but the model is "
                "slightly over-eager — it called a tool on small talk. "
                "Expect occasional unnecessary tool calls."
            ),
        }

    # One wrong_tool on disambiguation, otherwise clean.
    if len(failed) == 1 and wrong_tool:
        return {
            "label": "Workable",
            "tone": _VERDICT_WORKABLE,
            "reason": (
                "Tool-calling works but disambiguation is shaky. OK for "
                "single-tool flows; expect confusion when multiple "
                "relevant tools are offered."
            ),
        }

    # Multiple failures — not recommended.
    pct = round(parsed["pass_rate"] * 100)
    if pct < 60:
        return {
            "label": "Not recommended",
            "tone": _VERDICT_NOT_RECOMMENDED,
            "reason": (
                f"Only {parsed['passed']}/{parsed['total']} cases passed. Pick a different model."
            ),
        }

    return {
        "label": "Risky",
        "tone": _VERDICT_RISKY,
        "reason": (
            f"{len(failed)} case(s) failed across multiple patterns. "
            "Read the per-case detail below before binding."
        ),
    }


def _build_eval_context(request: Request, *, alias: str) -> dict[str, Any]:
    """Build the per-alias eval detail page context.

    Reads the rolling latest report at
    ``$FITT_HOME/eval/<alias>-latest.md`` (default suite) and
    ``$FITT_HOME/eval/<alias>-coding-latest.md`` (coding
    suite) and parses each via :func:`_parse_eval_report_full`.
    Each suite gets its own verdict so the operator gets
    bind-or-not signals per workload without having to read
    every case.

    Unknown aliases (typo in URL, or alias renamed since the
    report was written) still render — we don't gate on
    "alias is in config" because the report file itself is
    what matters. The page shows "No eval found" in the
    empty case.

    The CSRF token rides for the per-suite "Run again"
    buttons that POST to ``/dashboard/actions/run-eval``.
    """
    from .edit import issue_csrf as _issue_csrf

    home = _fitt_home()
    eval_dir = default_eval_dir(home)
    config = request.app.state.config
    alias_known = alias in config.alias_names()

    suites: list[dict[str, Any]] = []
    for suite_name, suffix, label in (
        ("default", "", "FITT default"),
        ("coding", "-coding", "Coding agent (router mode)"),
        ("realistic", "-realistic", "Realistic (FITT's live system prompt)"),
    ):
        report_path = eval_dir / f"{alias}{suffix}-latest.md"
        parsed = _parse_eval_report_full(report_path)
        verdict = _eval_verdict(parsed)
        suites.append(
            {
                "name": suite_name,
                "label": label,
                "report": parsed,
                "verdict": verdict,
                "report_path_human": str(report_path),
            }
        )

    auth = request.app.state.dashboard_auth
    csrf_token = _issue_csrf(request, key=auth.key())

    return {
        "alias": alias,
        "alias_known": alias_known,
        "suites": suites,
        "csrf_token": csrf_token,
        "client": getattr(request.state, "client", "webui"),
    }


# Probe statuses that are "environmental, not the binding's
# fault" — amber pip. Phase 7.6 Decision 8: the operator's
# question is "my problem or the model's problem"; cold-loading
# and rate-limits are transient/environmental, so they go amber
# rather than red. A genuinely-wrong binding (narrated/truncated/
# unreachable/auth) goes red.
_PROBE_AMBER_STATUSES: frozenset[str] = frozenset(
    {
        "upstream_silent",
        "upstream_rate_limited",
        "skipped_no_api_key",
    }
)


def _probe_pip(status: str | None) -> str:
    """Map a probe status to a pip colour class (Decision 8).

    green (``ok``) · amber (environmental: slow/cold-loading,
    rate-limited, skipped-no-key) · red (broken binding:
    narrated, truncated, unreachable, auth/client error, empty
    reply, server error) · grey (not probed)."""
    if status is None:
        return "unknown"
    if status == "ok":
        return "ok"
    if status in _PROBE_AMBER_STATUSES:
        return "warn"
    return "error"


def _probe_summary(probe: Any) -> str:
    """Compact one-glance probe summary for the aliases table
    (Decision 7): ``✓ 1.2s`` / ``… slow 10s+`` / ``✗ unreachable``
    / ``narrated`` / ``— not probed``. The full detail lives on
    the per-alias page; this is the index cell.

    Uses ``…`` rather than an hourglass emoji for the slow case:
    system monospace fonts (what the dashboard table renders in)
    lack the emoji glyph and show tofu, while ``✓`` / ``✗`` are
    Dingbats and render fine."""
    if probe is None:
        return "— not probed"
    status = str(probe.status)
    latency_ms = getattr(probe, "latency_ms", 0) or 0
    secs = latency_ms / 1000.0
    if status == "ok":
        return f"✓ {secs:.1f}s"
    if status == "upstream_silent":
        return f"… slow {secs:.0f}s+"
    if status == "unreachable":
        return "✗ unreachable"
    if status == "skipped_no_api_key":
        return "skipped"
    if status == "upstream_rate_limited":
        return "rate-limited"
    return status


def _shares_endpoint_with(config: Any, alias: str) -> tuple[str, list[str]]:
    """Return ``(endpoint, [other aliases on the same backend
    instance])`` for ``alias``'s primary model.

    "Same backend instance" uses :func:`alias_probe.endpoint_key`
    so the answer matches exactly what the sequential probe
    serialises on — the shared-GPU contention set. The other-
    aliases list is the "shares with" insight the per-alias page
    surfaces in context."""
    from ..alias_probe import endpoint_key

    primary = config.resolve_alias(alias)[0]
    my_key = endpoint_key(primary)
    others: list[str] = []
    for other in config.alias_names():
        if other == alias:
            continue
        other_primary = config.resolve_alias(other)[0]
        if endpoint_key(other_primary) == my_key:
            others.append(other)
    return primary.endpoint or "(no endpoint)", others


def _build_profile_view(alias: str) -> dict[str, Any] | None:
    """Shape the stored capability profile (``<alias>-profile.json``) for
    the alias page's Capability card.

    Returns ``None`` when no profile has been captured yet (the operator
    hasn't run ``fitt profile alias``), so the template shows an empty
    hint. Declared facts and measured grades are kept in separate lists
    (different trust levels), and capability (pass-rate) sits beside cost
    (latency, tokens) rather than blended - mirroring the profile's own
    design and its markdown render."""
    from ..capability_profile import load_baseline

    profile = load_baseline(alias, _fitt_home())
    if profile is None:
        return None

    def _rate(r: float | None) -> str:
        return f"{r * 100:.0f}%" if r is not None else "n/a"

    def _lat(s: float | None) -> str:
        return f"{s:.1f}s" if s is not None else "—"

    def _tok(t: float | None) -> str:
        return f"{t:.0f}" if t is not None else "—"

    measured: list[dict[str, Any]] = []
    for g in profile.measured:
        samples = f"{g.passes}/{g.valid}"
        if g.samples != g.valid:
            samples += f" (+{g.samples - g.valid} transient)"
        measured.append(
            {
                "name": g.name,
                "pass_rate": _rate(g.pass_rate),
                "samples": samples,
                "p50": _lat(g.p50_latency_s),
                "p95": _lat(g.p95_latency_s),
                "in_tok": _tok(g.avg_in_tokens),
                "out_tok": _tok(g.avg_out_tokens),
                "notes": g.notes,
            }
        )

    resource: dict[str, str | None] | None = None
    if profile.resource is not None:
        r = profile.resource
        resource = {
            "size": (
                f"{r.declared_size_bytes / 1_048_576:.0f} MB"
                if r.declared_size_bytes is not None
                else None
            ),
            "vram": f"{r.resident_vram_mb} MB" if r.resident_vram_mb is not None else None,
            "cold_load": f"{r.cold_load_s:.1f}s" if r.cold_load_s is not None else None,
        }

    return {
        "model_id": profile.model_id,
        "captured_at": profile.captured_at.strftime("%Y-%m-%d %H:%M UTC"),
        "declared": [
            {"name": f.name, "value": f.value, "source": f.source} for f in profile.declared
        ],
        "measured": measured,
        "resource": resource,
    }


def _build_alias_page_context(request: Request, *, alias: str) -> dict[str, Any]:
    """Assemble the unified per-alias page (Phase 7.6 Decision 6).

    One destination for "tell me about this binding": config
    (model / backend / fallback / endpoint), the "shares with"
    line (other aliases on the same backend instance — the
    shared-GPU insight), the full probe detail (status, latency,
    reachability verdict, narrated-reply preview), the three eval
    suites (reusing :func:`_build_eval_context`'s assembly), the
    context window, and the 24h dispatch count.

    Absorbs the F18 eval view — the eval suites render inline
    here instead of on a separate ``/dashboard/eval/<alias>``
    page (which now redirects here). The CSRF token rides for
    the per-suite "run eval" and the per-alias "re-probe"
    buttons."""
    from .edit import issue_csrf as _issue_csrf

    app = request.app
    config = app.state.config
    alias_known = alias in config.alias_names()

    # Config + endpoint + "shares with".
    model_human = backend = fallback_human = None
    endpoint = "(unknown)"
    shares_with: list[str] = []
    context_window_human = "?"
    context_source = "—"
    if alias_known:
        chain = config.resolve_alias(alias)
        primary = chain[0]
        fallback = chain[1] if len(chain) > 1 else None
        model_human = primary.model
        backend = primary.backend
        fallback_human = fallback.model if fallback else None
        endpoint, shares_with = _shares_endpoint_with(config, alias)

        cache = getattr(app.state, "context_windows", None)
        if cache is not None:
            cw = cache.get(primary.backend, primary.id)
            if cw is not None:
                context_window_human = _fmt_tokens(cw.tokens) if cw.tokens else "?"
                context_source = cw.source

    # Probe detail.
    probe_results: dict[str, Any] = getattr(app.state, "alias_probe_results", {}) or {}
    probe = probe_results.get(alias)
    probe_view: dict[str, Any] | None = None
    if probe is not None:
        probe_view = {
            "status": probe.status,
            "pip": _probe_pip(probe.status),
            "detail": getattr(probe, "detail", "") or "",
            "latency_ms": getattr(probe, "latency_ms", 0) or 0,
            "model_used": getattr(probe, "model_used", None),
            "finish_reason": getattr(probe, "finish_reason", None),
            "reply_preview": getattr(probe, "reply_preview", "") or "",
            "reachable": getattr(probe, "reachable", None),
        }
    probe_ran_at = getattr(app.state, "alias_probe_ran_at", None)
    probe_ran_at_human = (
        datetime.fromtimestamp(probe_ran_at, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        if probe_ran_at
        else None
    )

    # Eval suites — reuse the F18 assembly verbatim.
    eval_ctx = _build_eval_context(request, alias=alias)

    # 24h dispatches.
    audit = getattr(app.state, "audit", None)
    dispatch_counts = _count_dispatches_last_24h(audit)

    auth = app.state.dashboard_auth
    csrf_token = _issue_csrf(request, key=auth.key())

    return {
        "alias": alias,
        "alias_known": alias_known,
        "model": model_human,
        "backend": backend,
        "fallback": fallback_human,
        "endpoint": endpoint,
        "shares_with": shares_with,
        "context_window_human": context_window_human,
        "context_source": context_source,
        "probe": probe_view,
        "probe_ran_at_human": probe_ran_at_human,
        "suites": eval_ctx["suites"],
        "dispatched_24h": dispatch_counts.get(alias, 0),
        "profile": _build_profile_view(alias),
        "csrf_token": csrf_token,
        "client": getattr(request.state, "client", "webui"),
    }


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
        # Phase 7.6 (Decision 7): the table is a lean index. The
        # pip uses the amber/red split (environmental vs broken
        # binding); the probe cell is a compact summary
        # (✓ 1.2s / … slow 10s+ / ✗ unreachable) that links to
        # the per-alias page. The full detail (exception class,
        # narrated-reply preview, reachability facts) lives on
        # that page, not crammed into this cell — the F19
        # tooltip-stuffing is gone.
        pip = _probe_pip(probe.status if probe is not None else None)
        probe_summary = _probe_summary(probe)

        eval_report = _parse_eval_report(eval_dir / f"{alias}-latest.md")

        alias_rows.append(
            {
                "id": alias,
                "model": primary.model,
                "fallback": fallback.model if fallback else None,
                "backend": primary.backend,
                "endpoint": primary.endpoint or "—",
                "context_window_human": (_fmt_tokens(cw_tokens) if cw_tokens is not None else "?"),
                "context_source": cw_source,
                "pip": pip,
                "probe_summary": probe_summary,
                "last_eval_text": _format_eval_cell(alias, eval_report),
                "dispatched_24h": dispatch_counts.get(alias, 0),
            }
        )

    return {
        "alias_rows": alias_rows,
        "generated_at_human": _fmt_iso(now, mode="time"),
    }


def _aliases_context_with_csrf(request: Request) -> dict[str, Any]:
    """Aliases context plus a fresh CSRF token. Same data as
    :func:`_build_aliases_context` plus the token used by the
    F16 action buttons (refresh-aliases at the top, per-row
    run-eval). Used by both the page route and the partial
    refresh route so the buttons keep working across HTMX
    swaps."""
    from .edit import issue_csrf as _issue_csrf

    ctx = _build_aliases_context(request)
    auth = request.app.state.dashboard_auth
    ctx["csrf_token"] = _issue_csrf(request, key=auth.key())
    # Surface a one-shot banner when the action handler
    # redirected back here. The page route reads
    # banner_message + banner_color from the query string
    # and stuffs them into ctx; the partial route doesn't
    # see them (HTMX swaps in place without query strings)
    # so we set them empty here.
    ctx.setdefault("banner", None)
    return ctx


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

    plan = _reconstruct_plan_for_turn(request, session_key=session_key, turn_id=turn_id)

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
        "plan": plan,
        "dispatched_messages": messages,
        "tool_calls": tool_calls,
        "response_pretty": _json.dumps(cap.response, indent=2, ensure_ascii=False),
        "response_text": _flatten_response_text(cap.response),
        "day": day,
    }
    return {"session_key": session_key, "turn": turn}


_PLAN_STATUS_GLYPHS: dict[str, str] = {
    "done": "✅",
    "in_progress": "🔄",
    "blocked": "🚫",
    "pending": "⬜",
}


def _reconstruct_plan_for_turn(
    request: Request, *, session_key: str, turn_id: str
) -> dict[str, Any] | None:
    """Rebuild the turn's plan + final step statuses from the
    Phase 7 turn-event stream (Phase 12, Story 6.3 — reuse the
    existing substrate, no new store).

    Returns ``None`` when the turn emitted no ``plan_created`` event
    (an elected-out / flat-loop turn). Applies the step-started /
    step-completed events onto the created plan so the dashboard shows
    where execution actually got, and counts re-plans."""
    import time as _time

    turns_log = getattr(request.app.state, "turns", None)
    if turns_log is None:
        return None
    try:
        events = turns_log.read(session_key, turn_id=turn_id, now=_time.time())
    except Exception:
        return None

    created = next((e for e in events if e.kind == "plan_created"), None)
    if created is None:
        return None

    raw_items = created.meta.get("items") or []
    order: list[str] = []
    by_id: dict[str, dict[str, str]] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id", ""))
        order.append(sid)
        by_id[sid] = {
            "text": str(item.get("text", "")),
            "status": str(item.get("status", "pending")),
        }

    for e in events:
        sid = str((e.meta or {}).get("step_id", ""))
        if sid not in by_id:
            continue
        if e.kind == "plan_step_completed":
            by_id[sid]["status"] = "done"
        elif e.kind == "plan_step_started" and by_id[sid]["status"] != "done":
            by_id[sid]["status"] = "in_progress"

    replan_count = sum(1 for e in events if e.kind == "replan")
    items = [
        {
            "text": by_id[sid]["text"],
            "status": by_id[sid]["status"],
            "glyph": _PLAN_STATUS_GLYPHS.get(by_id[sid]["status"], "⬜"),
        }
        for sid in order
    ]
    done = sum(1 for i in items if i["status"] == "done")
    return {
        "items": items,
        "replan_count": replan_count,
        "done": done,
        "total": len(items),
    }


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


def _build_project_edit_context(
    request: Request,
    *,
    name: str,
    csrf_token: str,
    banner: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Look up one project for the edit form. Returns ``None``
    when the project doesn't exist; the route renders a 404
    in that case."""
    registry = getattr(request.app.state, "project_registry", None)
    if registry is None:
        return None
    try:
        project = registry.get(name)
    except Exception:
        return None
    return {
        "project": {
            "name": project.name,
            "path": project.path,
            "ssh_host": project.ssh_host,
            "test_command": project.test_command,
            "build_command": project.build_command,
        },
        "csrf_token": csrf_token,
        "banner": banner,
    }


async def _projects_action(
    request: Request,
    *,
    csrf_token: str,
    kind: str,
    name: str,
    fields: dict[str, str],
) -> Response:
    """Common handler for the three project-mutation POSTs.

    All three (add / update / remove) share the same auth +
    CSRF + audit flow; only the call to the registry differs.
    Centralising the shape means the route handlers stay
    boilerplate-free and the audit emission can't drift
    between them.
    """

    from .actions import ActionTimer, audit_action
    from .edit import CsrfMismatch, csrf_required

    guard = authorize_request(request)
    if guard is not None:
        return guard
    client = getattr(request.state, "client", "webui")
    audit_log = getattr(request.app.state, "audit", None)

    # CSRF check first.
    try:
        csrf_required(request, csrf_token)
    except CsrfMismatch as exc:
        audit_action(
            audit_log,
            tool=f"dashboard.project_{kind}",
            args={"name": name, **fields},
            client=client,
            ok=False,
            decision="rejected",
            error=str(exc),
            extra={"reason": "csrf_mismatch"},
        )
        return _projects_redirect(
            "CSRF token did not match — reload and try again",
            color="var(--error)",
        )

    if not name:
        audit_action(
            audit_log,
            tool=f"dashboard.project_{kind}",
            args={"name": name, **fields},
            client=client,
            ok=False,
            decision="rejected",
            error="missing project name",
            extra={"reason": "validation_failed"},
        )
        return _projects_redirect(
            "Project name is required",
            color="var(--error)",
        )

    registry = request.app.state.project_registry
    from ..projects import Project as _Project
    from ..projects import ProjectError as _ProjectError

    with ActionTimer() as timer:
        try:
            if kind == "add":
                project = _Project(
                    name=name,
                    path=fields.get("path", ""),
                    ssh_host=fields.get("ssh_host", ""),
                    test_command=fields.get("test_command", ""),
                    build_command=fields.get("build_command", ""),
                )
                registry.add(project)
                message = f"Added project {name!r}"
            elif kind == "update":
                # Filter out empty strings so we don't blank out
                # fields the operator left untouched.
                update_kwargs = {k: v for k, v in fields.items() if v != ""}
                # ssh_host / test / build are allowed to be
                # explicitly empty — but the form currently sends
                # the existing value, so empty really does mean
                # "clear this field." Pass them as-is.
                update_kwargs.update({k: v for k, v in fields.items() if k != "path"})
                # path always present (required field).
                if "path" in fields:
                    update_kwargs["path"] = fields["path"]
                registry.update(name, **update_kwargs)
                message = f"Updated project {name!r}"
            elif kind == "remove":
                registry.remove(name)
                message = f"Removed project {name!r}"
            else:  # pragma: no cover - defensive
                raise _ProjectError(f"unknown kind: {kind!r}")
        except _ProjectError as exc:
            audit_action(
                audit_log,
                tool=f"dashboard.project_{kind}",
                args={"name": name, **fields},
                client=client,
                ok=False,
                decision="rejected",
                error=str(exc),
                duration_ms=timer.elapsed_ms,
                extra={"reason": "validation_failed"},
            )
            return _projects_redirect(
                f"Project {kind} failed: {exc}",
                color="var(--error)",
            )
        except Exception as exc:
            audit_action(
                audit_log,
                tool=f"dashboard.project_{kind}",
                args={"name": name, **fields},
                client=client,
                ok=False,
                decision="error",
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=timer.elapsed_ms,
                extra={"reason": "io_error"},
            )
            return _projects_redirect(
                f"Project {kind} failed unexpectedly",
                color="var(--error)",
            )

    audit_action(
        audit_log,
        tool=f"dashboard.project_{kind}",
        args={"name": name, **fields},
        client=client,
        ok=True,
        decision="approved",
        duration_ms=timer.elapsed_ms,
    )
    return _projects_redirect(message, color="var(--accent)")


def _projects_redirect(message: str, *, color: str) -> Response:
    """Redirect to /dashboard/projects with a one-shot banner.

    The banner state lives in a tiny query-string flash; we
    don't write a cookie because the message is purely
    cosmetic. ``message`` is escaped at render time by Jinja's
    autoescape."""
    from urllib.parse import quote_plus as _quote

    from fastapi.responses import RedirectResponse as _Redirect

    target = f"/dashboard/projects?banner_message={_quote(message)}&banner_color={_quote(color)}"
    return _Redirect(url=target, status_code=303)


async def _cron_action(
    request: Request,
    *,
    csrf_token: str,
    kind: str,
    cron_id: str,
    enable: bool,
) -> Response:
    """Common handler for /cron/toggle and /cron/remove POSTs.

    Calls into :class:`gateway.cron.CronService` directly —
    the same code path the inline ``cron_*`` tools use. So a
    cron paused from the dashboard and a cron paused from a
    Telegram tool turn produce identical on-disk and
    audit-log effects.
    """
    from .actions import ActionTimer, audit_action
    from .edit import CsrfMismatch, csrf_required

    guard = authorize_request(request)
    if guard is not None:
        return guard
    client = getattr(request.state, "client", "webui")
    audit_log = getattr(request.app.state, "audit", None)

    try:
        csrf_required(request, csrf_token)
    except CsrfMismatch as exc:
        audit_action(
            audit_log,
            tool=f"dashboard.cron_{kind}",
            args={"cron_id": cron_id},
            client=client,
            ok=False,
            decision="rejected",
            error=str(exc),
            extra={"reason": "csrf_mismatch"},
        )
        return _cron_redirect(
            "CSRF token did not match — reload and try again",
            color="var(--error)",
        )

    if not cron_id:
        return _cron_redirect("Missing cron id", color="var(--error)")

    cron_service = request.app.state.cron
    from ..cron import UnknownCron as _UnknownCron

    with ActionTimer() as timer:
        try:
            if kind == "toggle":
                job = cron_service.update(cron_id, enabled=enable)
                message = f"{'Resumed' if enable else 'Paused'} cron {job.name!r}"
            elif kind == "remove":
                existed = cron_service.remove(cron_id)
                if not existed:
                    raise _UnknownCron(f"no cron with id {cron_id!r}")
                message = f"Removed cron {cron_id}"
            else:  # pragma: no cover
                raise ValueError(f"unknown cron action: {kind!r}")
        except _UnknownCron as exc:
            audit_action(
                audit_log,
                tool=f"dashboard.cron_{kind}",
                args={"cron_id": cron_id, "enable": enable},
                client=client,
                ok=False,
                decision="rejected",
                error=str(exc),
                duration_ms=timer.elapsed_ms,
                extra={"reason": "not_found"},
            )
            return _cron_redirect(
                f"Cron {cron_id!r} not found",
                color="var(--error)",
            )
        except Exception as exc:
            audit_action(
                audit_log,
                tool=f"dashboard.cron_{kind}",
                args={"cron_id": cron_id, "enable": enable},
                client=client,
                ok=False,
                decision="error",
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=timer.elapsed_ms,
                extra={"reason": "io_error"},
            )
            return _cron_redirect(
                f"Cron {kind} failed unexpectedly",
                color="var(--error)",
            )

    audit_action(
        audit_log,
        tool=f"dashboard.cron_{kind}",
        args={"cron_id": cron_id, "enable": enable},
        client=client,
        ok=True,
        decision="approved",
        duration_ms=timer.elapsed_ms,
    )
    return _cron_redirect(message, color="var(--accent)")


def _cron_redirect(message: str, *, color: str) -> Response:
    from urllib.parse import quote_plus as _quote

    from fastapi.responses import RedirectResponse as _Redirect

    target = f"/dashboard/cron?banner_message={_quote(message)}&banner_color={_quote(color)}"
    return _Redirect(url=target, status_code=303)


async def _secrets_save_action(
    request: Request,
    *,
    csrf_token: str,
    key: str,
    new_value: str,
    bearer_token: str,
) -> Response:
    """Common handler for the F15 secret-set POSTs.

    1. authorize_request — same cookie-or-bearer auth.
    2. CSRF check — F10 substrate.
    3. Bearer-token re-auth — F15 special. The session
       cookie alone is not enough authority for secrets
       writes; the operator pastes a bearer token in the
       form which is compared via ``compare_digest``
       against the configured allow-list.
    4. write_secret_field — atomic, audit-on-write,
       chmod-to-0600.
    5. Redirect with a flash banner. The banner_key
       parameter tells the rendered page which detail to
       open after the redirect.
    """
    from ..config import default_secrets_path
    from .actions import audit_action
    from .edit import CsrfMismatch, csrf_required
    from .secrets_edit import (
        SecretsEditError,
        UnknownKey,
        verify_bearer_reauth,
        write_secret_field,
    )

    guard = authorize_request(request)
    if guard is not None:
        return guard
    client = getattr(request.state, "client", "webui")
    audit_log = getattr(request.app.state, "audit", None)

    try:
        csrf_required(request, csrf_token)
    except CsrfMismatch:
        audit_action(
            audit_log,
            tool="dashboard.secret_set",
            args={"key": key},
            client=client,
            ok=False,
            decision="rejected",
            error="CSRF token mismatch",
            extra={"reason": "csrf_mismatch"},
        )
        return _secrets_redirect(
            "CSRF token did not match — reload and try again",
            color="var(--error)",
            key=None,
        )

    secrets = request.app.state.config.secrets
    if secrets is None:
        return _secrets_redirect(
            "Secrets not loaded; cannot edit",
            color="var(--error)",
            key=None,
        )

    if not verify_bearer_reauth(secrets, submitted=bearer_token):
        audit_action(
            audit_log,
            tool="dashboard.secret_set",
            args={"key": key},
            client=client,
            ok=False,
            decision="rejected",
            error="bearer re-auth failed",
            extra={"reason": "bearer_reauth_failed"},
        )
        return _secrets_redirect(
            "Bearer token did not match — reload and try again",
            color="var(--error)",
            key=key,
        )

    if not key:
        return _secrets_redirect("Missing key", color="var(--error)", key=None)

    path = default_secrets_path()
    try:
        write_secret_field(
            path=path,
            key=key,
            new_value=new_value,
            audit_log=audit_log,
            client=client,
        )
    except UnknownKey:
        return _secrets_redirect(f"Unknown key {key!r}", color="var(--error)", key=None)
    except SecretsEditError as exc:
        return _secrets_redirect(f"Save failed: {exc}", color="var(--error)", key=key)

    action = "set" if new_value else "unset"
    return _secrets_redirect(
        f"Saved {key} ({action}). Restart the gateway to apply.",
        color="var(--accent)",
        key=key,
    )


def _secrets_redirect(message: str, *, color: str, key: str | None) -> Response:
    from urllib.parse import quote_plus as _quote

    from fastapi.responses import RedirectResponse as _Redirect

    parts = [
        f"banner_message={_quote(message)}",
        f"banner_color={_quote(color)}",
    ]
    if key:
        parts.append(f"banner_key={_quote(key)}")
    target = "/dashboard/settings/secrets/edit?" + "&".join(parts)
    return _Redirect(url=target, status_code=303)


# --------------------------------------------------------------- typed actions (F16)


async def _action_refresh_aliases(request: Request) -> tuple[bool, str]:
    """Re-run context-window discovery for every alias."""
    cache = getattr(request.app.state, "context_windows", None)
    if cache is None:
        return False, "context cache not initialised"
    config = request.app.state.config
    timeout_s = float(getattr(config.server, "context_probe_timeout_s", 5.0))
    await cache.populate(config, timeout_s=timeout_s)
    return True, "Refreshed context windows for every alias"


async def _action_reprobe_aliases(request: Request) -> tuple[bool, str]:
    """Re-run the boot-time tool-call probe for every alias and
    refresh ``app.state.alias_probe_results`` in place.

    F20: the boot probe runs once at gateway start and caches.
    A binding that was unreachable at boot (Ollama still
    cold-loading weights, a satellite asleep) shows
    ``transport_error`` until the next restart. This action
    re-runs the probe against the live router so the operator
    can recover a stale ``transport_error`` without bouncing
    the gateway.

    Uses the same ``probe_all_aliases`` the boot path calls,
    same timeout config. Updates the in-process results dict
    and the ``alias_probe_ran_at`` timestamp so the aliases
    view reflects the new state on its next render."""
    import time as _time

    from ..alias_probe import probe_all_aliases
    from ..router import AliasRouter

    config = request.app.state.config
    timeout_s = float(getattr(config.server, "boot_probe_timeout_s", 10.0))
    router = AliasRouter(config)
    try:
        results = await probe_all_aliases(config, router, timeout_s=timeout_s)
    except Exception as exc:
        return False, f"Re-probe failed: {type(exc).__name__}: {exc}"

    request.app.state.alias_probe_results = {r.alias: r for r in results}
    request.app.state.alias_probe_ran_at = _time.time()

    ok_count = sum(1 for r in results if r.status == "ok")
    total = len(results)
    return True, f"Re-probed {total} alias(es) — {ok_count} ok"


async def _action_reprobe_alias(request: Request, *, alias: str) -> tuple[bool, str]:
    """Re-run the tool-call probe for ONE alias (Phase 7.6
    Decision 4). The per-alias companion to the re-probe-all
    button: probes just the binding the operator is debugging,
    so it gets the backend to itself and returns a clean single
    result without disturbing the siblings or waiting for a full
    sweep. Updates ``app.state.alias_probe_results[alias]`` in
    place."""
    import time as _time

    from ..alias_probe import probe_alias
    from ..router import AliasRouter

    if not alias:
        return False, "Missing alias"
    config = request.app.state.config
    if alias not in config.alias_names():
        return False, f"Unknown alias {alias!r}"

    timeout_s = float(getattr(config.server, "boot_probe_timeout_s", 10.0))
    router = AliasRouter(config)
    try:
        result = await probe_alias(alias, router, timeout_s=timeout_s, config=config)
    except Exception as exc:
        return False, f"Re-probe failed: {type(exc).__name__}: {exc}"

    results = getattr(request.app.state, "alias_probe_results", None)
    if isinstance(results, dict):
        results[alias] = result
    else:
        request.app.state.alias_probe_results = {alias: result}
    request.app.state.alias_probe_ran_at = _time.time()

    return True, f"Re-probed {alias} — {result.status} ({result.latency_ms} ms)"


async def _action_mcp_restart(request: Request, *, name: str) -> tuple[bool, str]:
    """Stop + start one MCP server by name."""
    if not name:
        return False, "Missing server name"
    manager = getattr(request.app.state, "mcp", None)
    registry = getattr(request.app.state, "tool_registry", None)
    if manager is None or registry is None:
        return False, "MCP not configured on this gateway"
    try:
        await manager.restart(name, registry)
    except KeyError:
        return False, f"No MCP server named {name!r}"
    except Exception as exc:
        return False, f"Restart failed: {exc}"
    return True, f"Restarted MCP server {name!r}"


async def _action_audit_verify(request: Request) -> tuple[bool, str]:
    """Walk the audit chain, re-compute every HMAC."""
    audit = getattr(request.app.state, "audit", None)
    if audit is None:
        return False, "Audit log not configured"
    result = audit.verify()
    if result.ok:
        return True, f"Audit chain verified — {result.total_lines} entries"
    return (
        False,
        f"Audit chain BROKEN at line {result.bad_line}: {result.reason}",
    )


async def _action_pruner_tick(request: Request, *, which: str) -> tuple[bool, str]:
    """Force a pruner sweep right now."""
    if which == "history":
        pruner = getattr(request.app.state, "history_pruner", None)
        label = "history"
    elif which == "events":
        pruner = getattr(request.app.state, "event_pruner", None)
        label = "events"
    else:
        return False, f"Unknown pruner {which!r}"
    if pruner is None:
        return False, f"{label} pruner not configured"
    try:
        removed = await pruner.tick(now=None)
    except Exception as exc:
        return False, f"{label} pruner failed: {exc}"
    if removed is None:
        return True, f"{label} pruner ran (nothing was due)"
    return True, f"{label} pruner removed {removed} item(s)"


async def _action_run_eval(
    request: Request, *, alias: str, suite: str = "default"
) -> tuple[bool, str]:
    """Run the eval suite for one alias — same code path
    POST /v1/eval/<alias>?suite=<...> uses."""
    if not alias:
        return False, "Missing alias"
    config = request.app.state.config
    if alias not in config.aliases:
        return False, f"Unknown alias {alias!r}"
    from ..alias_eval import (
        default_cases,
        realistic_cases,
        run_eval_suite,
        write_report,
    )
    from ..alias_eval_coding import default_coding_cases
    from ..eval_endpoint import build_realistic_system_prompt
    from ..router import AliasRouter

    realistic_prompt = ""
    realistic_meta: dict[str, Any] = {}
    if suite == "default":
        cases = default_cases()
    elif suite == "coding":
        cases = default_coding_cases()
    elif suite == "realistic":
        cases = realistic_cases()
        realistic_prompt, realistic_meta = build_realistic_system_prompt(request.app.state)
    else:
        return False, f"Unknown suite {suite!r}"

    eval_router = AliasRouter(config)
    try:
        report = await run_eval_suite(
            alias, eval_router, cases=cases, system_prompt=realistic_prompt
        )
    except Exception as exc:
        return False, f"Eval failed: {type(exc).__name__}: {exc}"

    extra_header_lines: list[str] = []
    if suite == "realistic":
        approx = realistic_meta.get("approx_tokens", 0)
        comps = ", ".join(realistic_meta.get("components", [])) or "(none)"
        extra_header_lines.append(
            f"- Realistic prompt: ~{approx} tokens "
            f"({realistic_meta.get('chars', 0)} chars; components: {comps})"
        )
    try:
        from ..config import fitt_home as _fh

        write_report(report, _fh(), suite=suite, extra_header_lines=extra_header_lines)
    except Exception:
        # Persistence failure shouldn't fail the action;
        # the report's already in memory and the audit
        # trail captures the run.
        pass
    suffix = ""
    if suite == "realistic" and realistic_meta:
        suffix = f" (prompt ~{realistic_meta.get('approx_tokens', 0)} tokens)"
    return True, (
        f"Eval ({suite}) ran — {report.passed}/{report.total} "
        f"passed ({report.pass_rate:.0%}){suffix}"
    )


async def _action_profile_alias(request: Request, *, alias: str) -> tuple[bool, str]:
    """Build a capability profile for ONE alias from the dashboard
    — the same producer ``POST /v1/profile/<alias>`` uses. Writes
    under the gateway's ``$FITT_HOME/eval/`` so the Capability card
    picks it up (no host-vs-container path mismatch — the gateway
    runs it).

    Uses a modest sample count to keep the synchronous dashboard run
    bounded (the planner pass is slow on thinking models); the CLI /
    endpoint default to more samples for a sharper read."""
    if not alias:
        return False, "Missing alias"
    config = request.app.state.config
    if alias not in config.aliases:
        return False, f"Unknown alias {alias!r}"
    from ..capability_profile import write_profile
    from ..config import fitt_home as _fh
    from ..profile_runner import run_profile

    try:
        profile = await run_profile(
            alias=alias, cfg=config, state=request.app.state, samples=3, timeout_s=30.0
        )
    except Exception as exc:
        return False, f"Profile failed: {type(exc).__name__}: {exc}"
    try:
        write_profile(profile, _fh())
    except Exception:
        # Persistence failure shouldn't fail the action; the audit
        # trail captures the run.
        pass
    grades = ", ".join(
        f"{g.name} {g.pass_rate * 100:.0f}%" for g in profile.measured if g.pass_rate is not None
    )
    return True, f"Profiled {alias} — {grades or 'no gradeable signal'}"


def _action_redirect(target: str, message: str, *, color: str) -> Response:
    """Redirect to ``target`` with a one-shot banner."""
    from urllib.parse import quote_plus as _quote

    from fastapi.responses import RedirectResponse as _Redirect

    sep = "&" if "?" in target else "?"
    url = f"{target}{sep}banner_message={_quote(message)}&banner_color={_quote(color)}"
    return _Redirect(url=url, status_code=303)


async def _run_typed_action(
    request: Request,
    *,
    csrf_token: str,
    action_name: str,
    action_args: dict[str, Any],
    redirect_target: str,
    run: Any,
) -> Response:
    """Common handler for the F16 button-driven typed POSTs.

    Each route hands in:

    * the submitted CSRF token,
    * an action name (used as the audit ``tool`` namespace),
    * an args dict (rendered into the audit entry),
    * the redirect target on success/failure,
    * a zero-arg async callable that does the work and
      returns ``(ok: bool, message: str)``.

    The helper handles auth, CSRF, audit-on-success,
    audit-on-failure, and the redirect-with-banner. Each
    action stays a one-method-call function; the bookkeeping
    lives here.

    No generic command runner. ``run`` is a typed Python
    callable produced by the route handler — there's no
    string interpolation, no shell, no eval. The action's
    surface is fully constrained by its named function.
    """
    from .actions import ActionTimer, audit_action
    from .edit import CsrfMismatch, csrf_required

    guard = authorize_request(request)
    if guard is not None:
        return guard
    client = getattr(request.state, "client", "webui")
    audit_log = getattr(request.app.state, "audit", None)

    tool_name = f"dashboard.action.{action_name}"

    try:
        csrf_required(request, csrf_token)
    except CsrfMismatch:
        audit_action(
            audit_log,
            tool=tool_name,
            args=action_args,
            client=client,
            ok=False,
            decision="rejected",
            error="CSRF token mismatch",
            extra={"reason": "csrf_mismatch"},
        )
        return _action_redirect(
            redirect_target,
            "CSRF token did not match — reload and try again",
            color="var(--error)",
        )

    with ActionTimer() as timer:
        try:
            ok, message = await run()
        except Exception as exc:
            audit_action(
                audit_log,
                tool=tool_name,
                args=action_args,
                client=client,
                ok=False,
                decision="error",
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=timer.elapsed_ms,
                extra={"reason": "exception"},
            )
            return _action_redirect(
                redirect_target,
                f"Action failed: {type(exc).__name__}",
                color="var(--error)",
            )

    audit_action(
        audit_log,
        tool=tool_name,
        args=action_args,
        client=client,
        ok=ok,
        decision="approved" if ok else "rejected",
        error="" if ok else message,
        duration_ms=timer.elapsed_ms,
    )
    return _action_redirect(
        redirect_target,
        message,
        color="var(--accent)" if ok else "var(--error)",
    )


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
                    "created_iso": _fmt_iso(s.created_at.timestamp()),
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


_IDENTITY_FILENAMES: tuple[str, ...] = ("user.md", "soul.md", "tools.md", "lessons.md")
"""Editable identity files. Tightly scoped — the form's
filename hidden input must be one of these. Anything else
is rejected outright; we don't trust user-supplied paths
under any circumstance."""


def _identity_path(request: Request, filename: str) -> Path | None:
    """Resolve a filename to its on-disk path, refusing
    anything outside the configured identity dir."""
    if filename not in _IDENTITY_FILENAMES:
        return None
    config = request.app.state.config
    return Path(config.memory.identity_dir) / filename


def _skill_path(request: Request, skill_name: str) -> Path | None:
    """Resolve a skill name to its SKILL.md path under the
    configured skills_dir.

    The name is allowlisted against the loaded skills to
    refuse path-traversal payloads. We don't allow creating
    new skills via this surface — the F13 commit's contract
    is "edit existing"; create-new rides a follow-up if it
    earns its weight. Any unknown name returns None so the
    route handler can 302 the operator back to the listing.
    """
    if not skill_name:
        return None
    if "/" in skill_name or "\\" in skill_name or skill_name.startswith("."):
        return None
    skills = getattr(request.app.state, "skills", None) or []
    valid_names = {s.name for s in skills}
    if skill_name not in valid_names:
        return None
    config = request.app.state.config
    return Path(config.memory.skills_dir) / skill_name / "SKILL.md"


def _build_identity_edit_context(
    request: Request,
    *,
    filename: str,
    csrf_token: str,
    content: str | None = None,
    expected_mtime: float | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
    current_mtime: float | None = None,
    submitted_mtime: float | None = None,
    saved_at: float | None = None,
    bytes_written: int | None = None,
    bytes_changed_delta: int | None = None,
) -> dict[str, Any]:
    """Build the context for ``identity_edit.html``.

    Used both by the GET (render existing content) and the
    POST (re-render after save / failure with the operator's
    submitted bytes preserved). Keeping one builder means
    the template's contract stays simple and the route's
    failure paths can re-render without re-reading disk.
    """
    path = _identity_path(request, filename)
    if path is None:
        # Caller checks; this is just defensive.
        return {
            "filename": filename,
            "file_path": "<invalid>",
            "csrf_token": csrf_token,
            "content": "",
            "expected_mtime": "",
            "error_code": "validation_failed",
            "error_detail": f"unknown identity file: {filename}",
        }
    if content is None:
        # Fresh GET — read from disk.
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as exc:
                content = ""
                error_code = "io_error"
                error_detail = str(exc)
            else:
                expected_mtime = path.stat().st_mtime
        else:
            content = ""
            expected_mtime = None
    return {
        "filename": filename,
        "file_path": str(path),
        "csrf_token": csrf_token,
        "content": content,
        "expected_mtime": expected_mtime if expected_mtime is not None else "",
        "error_code": error_code,
        "error_detail": error_detail,
        "current_mtime": current_mtime,
        "submitted_mtime": submitted_mtime,
        "saved_at": saved_at,
        "bytes_written": bytes_written,
        "bytes_changed_delta": bytes_changed_delta,
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
        "generated_at_human": _fmt_iso(now, mode="time"),
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
        "generated_at_human": _fmt_iso(now, mode="time"),
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
        "generated_at_human": _fmt_iso(now, mode="time"),
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
    async def aliases_view(
        request: Request,
        banner_message: str | None = None,
        banner_color: str | None = None,
    ) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _aliases_context_with_csrf(request)
        ctx["client"] = getattr(request.state, "client", "webui")
        if banner_message:
            ctx["banner"] = {
                "message": banner_message,
                "color": banner_color or "var(--accent)",
            }
        return templates.TemplateResponse(request, "aliases.html", ctx)

    @router.get("/_partials/aliases", response_class=HTMLResponse)
    async def aliases_partial(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _aliases_context_with_csrf(request)
        return templates.TemplateResponse(request, "_aliases_panel.html", ctx)

    # --------------------------------------------------------- F18 + 7.6: per-alias page

    @router.get("/alias/{alias}", response_class=HTMLResponse)
    async def alias_page_view(request: Request, alias: str) -> Response:
        """Unified per-alias page (Phase 7.6 Decision 6).

        One destination for "tell me about this binding":
        config + endpoint + "shares with", the full probe
        detail (status, latency, reachability, narrated-reply
        preview), the three eval suites (absorbing the former
        F18 ``/dashboard/eval/<alias>`` view), context window,
        and 24h dispatches. Action buttons: per-suite run-eval
        and a per-alias re-probe."""
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_alias_page_context(request, alias=alias)
        return templates.TemplateResponse(request, "alias_page.html", ctx)

    @router.get("/eval/{alias}", response_class=HTMLResponse)
    async def eval_view(request: Request, alias: str) -> Response:
        """Phase 7.6: the standalone eval view is absorbed into
        the unified per-alias page. Redirect to it (anchored at
        the eval section) so old links / bookmarks keep working
        and no eval information is lost."""
        guard = authorize_request(request)
        if guard is not None:
            return guard
        from fastapi.responses import RedirectResponse as _Redirect

        return _Redirect(url=f"/dashboard/alias/{alias}#eval", status_code=307)

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

    @router.get("/_partials/turns", response_class=HTMLResponse)
    async def turns_partial(
        request: Request,
        session: str = "main",
        limit: int = 50,
    ) -> Response:
        """HTMX-driven 5s refresh for the turns list. Renders
        the table fragment only — F17's polling-as-live shape.
        SSE-driven prepend (the original Slice 7.5 Task 26c
        spec) rides a follow-up; polling covers the v0 use
        case."""
        guard = authorize_request(request)
        if guard is not None:
            return guard
        key = _safe_session_key(session)
        clamped = max(1, min(limit, 200))
        ctx = _build_turn_list_context(request, session_key=key, limit=clamped)
        return templates.TemplateResponse(request, "_turns_list_panel.html", ctx)

    @router.get("/tools", response_class=HTMLResponse)
    async def tools_view(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_tools_context(request)
        ctx["client"] = getattr(request.state, "client", "webui")
        return templates.TemplateResponse(request, "tools.html", ctx)

    @router.get("/cron", response_class=HTMLResponse)
    async def cron_view(
        request: Request,
        banner_message: str | None = None,
        banner_color: str | None = None,
    ) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_cron_context(request)
        ctx["client"] = getattr(request.state, "client", "webui")
        from .edit import issue_csrf as _issue_csrf

        auth = request.app.state.dashboard_auth
        ctx["csrf_token"] = _issue_csrf(request, key=auth.key())
        if banner_message:
            ctx["banner"] = {
                "message": banner_message,
                "color": banner_color or "var(--accent)",
            }
        else:
            ctx["banner"] = None
        return templates.TemplateResponse(request, "cron.html", ctx)

    @router.post("/cron/toggle", response_class=HTMLResponse)
    async def cron_toggle(
        request: Request,
        csrf_token: str = Form(""),
        cron_id: str = Form(""),
        enable: str = Form("1"),
    ) -> Response:
        return await _cron_action(
            request,
            csrf_token=csrf_token,
            kind="toggle",
            cron_id=cron_id.strip(),
            enable=(enable.strip() == "1"),
        )

    @router.post("/cron/remove", response_class=HTMLResponse)
    async def cron_remove(
        request: Request,
        csrf_token: str = Form(""),
        cron_id: str = Form(""),
    ) -> Response:
        return await _cron_action(
            request,
            csrf_token=csrf_token,
            kind="remove",
            cron_id=cron_id.strip(),
            enable=False,
        )

    @router.get("/audit", response_class=HTMLResponse)
    async def audit_view(
        request: Request,
        tool: str | None = None,
        limit: int = 100,
        banner_message: str | None = None,
        banner_color: str | None = None,
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
        from .edit import issue_csrf as _issue_csrf

        auth = request.app.state.dashboard_auth
        ctx["csrf_token"] = _issue_csrf(request, key=auth.key())
        if banner_message:
            ctx["banner"] = {
                "message": banner_message,
                "color": banner_color or "var(--accent)",
            }
        else:
            ctx["banner"] = None
        return templates.TemplateResponse(request, "audit.html", ctx)

    @router.get("/health", response_class=HTMLResponse)
    async def health_view(
        request: Request,
        banner_message: str | None = None,
        banner_color: str | None = None,
    ) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_health_context(request)
        ctx["client"] = getattr(request.state, "client", "webui")
        from .edit import issue_csrf as _issue_csrf

        auth = request.app.state.dashboard_auth
        ctx["csrf_token"] = _issue_csrf(request, key=auth.key())
        if banner_message:
            ctx["banner"] = {
                "message": banner_message,
                "color": banner_color or "var(--accent)",
            }
        else:
            ctx["banner"] = None
        return templates.TemplateResponse(request, "health.html", ctx)

    @router.get("/_partials/health", response_class=HTMLResponse)
    async def health_partial(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_health_context(request)
        from .edit import issue_csrf as _issue_csrf

        auth = request.app.state.dashboard_auth
        ctx["csrf_token"] = _issue_csrf(request, key=auth.key())
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

    @router.get("/settings/config/edit", response_class=HTMLResponse)
    async def settings_config_edit_get(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        from ..config import default_config_path
        from .edit import issue_csrf as _issue_csrf

        path = default_config_path()
        auth = request.app.state.dashboard_auth
        token = _issue_csrf(request, key=auth.key())

        content = ""
        expected_mtime: float | str = ""
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
                expected_mtime = path.stat().st_mtime
            except OSError:
                pass

        ctx = {
            "file_path": str(path),
            "csrf_token": token,
            "content": content,
            "expected_mtime": expected_mtime,
            "client": getattr(request.state, "client", "webui"),
        }
        return templates.TemplateResponse(request, "config_edit.html", ctx)

    @router.post("/settings/config/save", response_class=HTMLResponse)
    async def settings_config_save(
        request: Request,
        csrf_token: str = Form(""),
        expected_mtime: str = Form(""),
        content: str = Form(""),
    ) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        client = getattr(request.state, "client", "webui")

        from ..config import default_config_path, validate_config_yaml
        from .edit import (
            CsrfMismatch,
            MtimeConflict,
            ValidationFailed,
            csrf_required,
            issue_csrf,
            save_file_with_mtime,
        )

        auth = request.app.state.dashboard_auth
        next_token = issue_csrf(request, key=auth.key())
        path = default_config_path()

        # CSRF first.
        try:
            csrf_required(request, csrf_token)
        except CsrfMismatch:
            ctx = {
                "file_path": str(path),
                "csrf_token": next_token,
                "content": content,
                "expected_mtime": expected_mtime,
                "error_code": "csrf_mismatch",
                "client": client,
            }
            return templates.TemplateResponse(request, "config_edit.html", ctx, status_code=403)

        expected_ts: float | None
        if expected_mtime.strip():
            try:
                expected_ts = float(expected_mtime)
            except ValueError:
                expected_ts = None
        else:
            expected_ts = None

        audit_log = getattr(request.app.state, "audit", None)

        try:
            result = save_file_with_mtime(
                path=path,
                new_content=content,
                expected_mtime=expected_ts,
                audit_log=audit_log,
                client=client,
                validate=validate_config_yaml,
            )
        except MtimeConflict:
            ctx = {
                "file_path": str(path),
                "csrf_token": next_token,
                "content": content,
                "expected_mtime": expected_ts or "",
                "error_code": "mtime_conflict",
                "client": client,
            }
            return templates.TemplateResponse(request, "config_edit.html", ctx, status_code=409)
        except ValidationFailed as exc:
            ctx = {
                "file_path": str(path),
                "csrf_token": next_token,
                "content": content,
                "expected_mtime": expected_ts or "",
                "error_code": "validation_failed",
                "error_detail": exc.detail,
                "client": client,
            }
            return templates.TemplateResponse(request, "config_edit.html", ctx, status_code=400)
        except Exception:
            ctx = {
                "file_path": str(path),
                "csrf_token": next_token,
                "content": content,
                "expected_mtime": expected_ts or "",
                "error_code": "io_error",
                "client": client,
            }
            return templates.TemplateResponse(request, "config_edit.html", ctx, status_code=500)

        ctx = {
            "file_path": str(path),
            "csrf_token": next_token,
            "content": content,
            "expected_mtime": result.new_mtime,
            "saved_at": time.time(),
            "bytes_written": result.bytes_written,
            "client": client,
        }
        return templates.TemplateResponse(request, "config_edit.html", ctx)

    # --------------------------------------------------------- secrets edit (F15)

    @router.get("/settings/secrets/edit", response_class=HTMLResponse)
    async def settings_secrets_edit_get(
        request: Request,
        banner_message: str | None = None,
        banner_color: str | None = None,
        banner_key: str | None = None,
    ) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        from ..config import default_secrets_path
        from .edit import issue_csrf as _issue_csrf
        from .secrets_edit import _SCALAR_KEYS, _TELEGRAM_KEY, secret_presence

        auth = request.app.state.dashboard_auth
        token = _issue_csrf(request, key=auth.key())
        secrets = request.app.state.config.secrets

        if secrets is None:
            ctx = {
                "file_path": str(default_secrets_path()),
                "csrf_token": token,
                "scalar_keys": [(k, "not configured") for k in _SCALAR_KEYS],
                "api_keys_entries": [],
                "telegram_status": "not configured",
                "banner": {
                    "message": "secrets.yaml is not loaded",
                    "color": "var(--warn)",
                },
                "banner_key": None,
                "client": getattr(request.state, "client", "webui"),
            }
            return templates.TemplateResponse(request, "secrets_edit.html", ctx, status_code=503)

        presence = secret_presence(secrets)
        scalar_keys = [(k, presence.get(k, "not configured")) for k in _SCALAR_KEYS]
        api_keys_entries = sorted((model_id, "configured") for model_id in (secrets.api_keys or {}))
        ctx = {
            "file_path": str(default_secrets_path()),
            "csrf_token": token,
            "scalar_keys": scalar_keys,
            "api_keys_entries": api_keys_entries,
            "telegram_status": presence.get(_TELEGRAM_KEY, "not configured"),
            "banner": (
                {"message": banner_message, "color": banner_color or "var(--accent)"}
                if banner_message
                else None
            ),
            "banner_key": banner_key,
            "client": getattr(request.state, "client", "webui"),
        }
        return templates.TemplateResponse(request, "secrets_edit.html", ctx)

    @router.post("/settings/secrets/save", response_class=HTMLResponse)
    async def settings_secrets_save(
        request: Request,
        csrf_token: str = Form(""),
        key: str = Form(""),
        new_value: str = Form(""),
        bearer_token: str = Form(""),
    ) -> Response:
        return await _secrets_save_action(
            request,
            csrf_token=csrf_token,
            key=key.strip(),
            new_value=new_value,
            bearer_token=bearer_token,
        )

    @router.post("/settings/secrets/save_new_api_key", response_class=HTMLResponse)
    async def settings_secrets_save_new_api_key(
        request: Request,
        csrf_token: str = Form(""),
        model_id: str = Form(""),
        new_value: str = Form(""),
        bearer_token: str = Form(""),
    ) -> Response:
        # The "add new" form takes a model_id and constructs
        # the key path. Same audit + bearer-reauth path as
        # the per-key save.
        model_id = model_id.strip()
        if not model_id or "/" in model_id or "\\" in model_id:
            return _secrets_redirect(
                "Invalid model id",
                color="var(--error)",
                key=None,
            )
        return await _secrets_save_action(
            request,
            csrf_token=csrf_token,
            key=f"api_keys.{model_id}",
            new_value=new_value,
            bearer_token=bearer_token,
        )

    @router.get("/projects", response_class=HTMLResponse)
    async def projects_view(
        request: Request,
        banner_message: str | None = None,
        banner_color: str | None = None,
    ) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_projects_context(request)
        ctx["client"] = getattr(request.state, "client", "webui")
        from .edit import issue_csrf as _issue_csrf

        auth = request.app.state.dashboard_auth
        ctx["csrf_token"] = _issue_csrf(request, key=auth.key())
        if banner_message:
            ctx["banner"] = {
                "message": banner_message,
                "color": banner_color or "var(--accent)",
            }
        else:
            ctx["banner"] = None
        return templates.TemplateResponse(request, "projects.html", ctx)

    @router.get("/projects/edit", response_class=HTMLResponse)
    async def projects_edit_get(
        request: Request,
        name: str = "",
    ) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        from fastapi.responses import RedirectResponse as _Redirect

        from .edit import issue_csrf as _issue_csrf

        auth = request.app.state.dashboard_auth
        token = _issue_csrf(request, key=auth.key())
        ctx = _build_project_edit_context(request, name=name, csrf_token=token)
        if ctx is None:
            return _Redirect(url="/dashboard/projects", status_code=302)
        ctx["client"] = getattr(request.state, "client", "webui")
        return templates.TemplateResponse(request, "projects_edit.html", ctx)

    @router.post("/projects/add", response_class=HTMLResponse)
    async def projects_add(
        request: Request,
        csrf_token: str = Form(""),
        name: str = Form(""),
        path: str = Form(""),
        ssh_host: str = Form(""),
        test_command: str = Form(""),
        build_command: str = Form(""),
    ) -> Response:
        return await _projects_action(
            request,
            csrf_token=csrf_token,
            kind="add",
            name=name.strip(),
            fields={
                "path": path.strip(),
                "ssh_host": ssh_host.strip(),
                "test_command": test_command.strip(),
                "build_command": build_command.strip(),
            },
        )

    @router.post("/projects/update", response_class=HTMLResponse)
    async def projects_update(
        request: Request,
        csrf_token: str = Form(""),
        name: str = Form(""),
        path: str = Form(""),
        ssh_host: str = Form(""),
        test_command: str = Form(""),
        build_command: str = Form(""),
    ) -> Response:
        return await _projects_action(
            request,
            csrf_token=csrf_token,
            kind="update",
            name=name.strip(),
            fields={
                "path": path.strip(),
                "ssh_host": ssh_host.strip(),
                "test_command": test_command.strip(),
                "build_command": build_command.strip(),
            },
        )

    @router.post("/projects/remove", response_class=HTMLResponse)
    async def projects_remove(
        request: Request,
        csrf_token: str = Form(""),
        name: str = Form(""),
    ) -> Response:
        return await _projects_action(
            request,
            csrf_token=csrf_token,
            kind="remove",
            name=name.strip(),
            fields={},
        )

    @router.get("/identity", response_class=HTMLResponse)
    async def identity_view(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_identity_context(request)
        ctx["client"] = getattr(request.state, "client", "webui")
        return templates.TemplateResponse(request, "identity.html", ctx)

    @router.get("/identity/edit", response_class=HTMLResponse)
    async def identity_edit_get(
        request: Request,
        filename: str = "",
    ) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        if filename not in _IDENTITY_FILENAMES:
            from fastapi.responses import RedirectResponse as _Redirect

            return _Redirect(url="/dashboard/identity", status_code=302)
        from .edit import issue_csrf as _issue_csrf

        auth = request.app.state.dashboard_auth
        token = _issue_csrf(request, key=auth.key())
        ctx = _build_identity_edit_context(
            request,
            filename=filename,
            csrf_token=token,
        )
        ctx["client"] = getattr(request.state, "client", "webui")
        return templates.TemplateResponse(request, "identity_edit.html", ctx)

    @router.post("/identity/save", response_class=HTMLResponse)
    async def identity_edit_save(
        request: Request,
        csrf_token: str = Form(""),
        filename: str = Form(""),
        expected_mtime: str = Form(""),
        content: str = Form(""),
    ) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        client = getattr(request.state, "client", "webui")

        # Re-issue a fresh token for the next render no matter
        # which path we take. The submitted token might be
        # stale by the time we re-render.
        from .edit import (
            CsrfMismatch,
            MtimeConflict,
            ValidationFailed,
            csrf_required,
            issue_csrf,
            save_file_with_mtime,
        )

        auth = request.app.state.dashboard_auth
        next_token = issue_csrf(request, key=auth.key())

        if filename not in _IDENTITY_FILENAMES:
            ctx = _build_identity_edit_context(
                request,
                filename=filename,
                csrf_token=next_token,
                content=content,
                error_code="validation_failed",
                error_detail=f"unknown identity file: {filename!r}",
            )
            ctx["client"] = client
            return templates.TemplateResponse(request, "identity_edit.html", ctx, status_code=400)

        # CSRF first — independent of any disk state.
        try:
            csrf_required(request, csrf_token)
        except CsrfMismatch:
            ctx = _build_identity_edit_context(
                request,
                filename=filename,
                csrf_token=next_token,
                content=content,
                error_code="csrf_mismatch",
            )
            ctx["client"] = client
            return templates.TemplateResponse(request, "identity_edit.html", ctx, status_code=403)

        # Parse the mtime hint. Empty string == "didn't exist
        # at render time"; pass through as None.
        expected_ts: float | None
        if expected_mtime.strip():
            try:
                expected_ts = float(expected_mtime)
            except ValueError:
                expected_ts = None
        else:
            expected_ts = None

        path = _identity_path(request, filename)
        # _identity_path None case already filtered by the
        # filename check above; defensive assertion.
        assert path is not None

        audit_log = getattr(request.app.state, "audit", None)

        try:
            result = save_file_with_mtime(
                path=path,
                new_content=content,
                expected_mtime=expected_ts,
                audit_log=audit_log,
                client=client,
            )
        except MtimeConflict as exc:
            ctx = _build_identity_edit_context(
                request,
                filename=filename,
                csrf_token=next_token,
                content=content,
                error_code="mtime_conflict",
                current_mtime=exc.current_mtime,
                submitted_mtime=expected_ts,
                expected_mtime=expected_ts,
            )
            ctx["client"] = client
            return templates.TemplateResponse(request, "identity_edit.html", ctx, status_code=409)
        except ValidationFailed as exc:
            ctx = _build_identity_edit_context(
                request,
                filename=filename,
                csrf_token=next_token,
                content=content,
                error_code="validation_failed",
                error_detail=exc.detail,
                expected_mtime=expected_ts,
            )
            ctx["client"] = client
            return templates.TemplateResponse(request, "identity_edit.html", ctx, status_code=400)
        except Exception:
            ctx = _build_identity_edit_context(
                request,
                filename=filename,
                csrf_token=next_token,
                content=content,
                error_code="io_error",
                expected_mtime=expected_ts,
            )
            ctx["client"] = client
            return templates.TemplateResponse(request, "identity_edit.html", ctx, status_code=500)

        # Success — re-render with the new content + a fresh
        # mtime hint so consecutive saves work.
        ctx = _build_identity_edit_context(
            request,
            filename=filename,
            csrf_token=next_token,
            content=content,
            expected_mtime=result.new_mtime,
            saved_at=time.time(),
            bytes_written=result.bytes_written,
            bytes_changed_delta=result.bytes_changed_delta,
        )
        ctx["client"] = client
        return templates.TemplateResponse(request, "identity_edit.html", ctx)

    @router.get("/skills", response_class=HTMLResponse)
    async def skills_view(request: Request) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        ctx = _build_skills_context(request)
        ctx["client"] = getattr(request.state, "client", "webui")
        return templates.TemplateResponse(request, "skills.html", ctx)

    @router.get("/skills/edit", response_class=HTMLResponse)
    async def skills_edit_get(
        request: Request,
        skill_name: str = "",
    ) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        from fastapi.responses import RedirectResponse as _Redirect

        path = _skill_path(request, skill_name)
        if path is None:
            return _Redirect(url="/dashboard/skills", status_code=302)

        from .edit import issue_csrf as _issue_csrf

        auth = request.app.state.dashboard_auth
        token = _issue_csrf(request, key=auth.key())

        content = ""
        expected_mtime: float | str = ""
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
                expected_mtime = path.stat().st_mtime
            except OSError:
                pass
        ctx = {
            "skill_name": skill_name,
            "file_path": str(path),
            "csrf_token": token,
            "content": content,
            "expected_mtime": expected_mtime,
            "client": getattr(request.state, "client", "webui"),
        }
        return templates.TemplateResponse(request, "skills_edit.html", ctx)

    @router.post("/skills/save", response_class=HTMLResponse)
    async def skills_edit_save(
        request: Request,
        csrf_token: str = Form(""),
        skill_name: str = Form(""),
        expected_mtime: str = Form(""),
        content: str = Form(""),
    ) -> Response:
        guard = authorize_request(request)
        if guard is not None:
            return guard
        client = getattr(request.state, "client", "webui")

        from .edit import (
            CsrfMismatch,
            MtimeConflict,
            ValidationFailed,
            csrf_required,
            issue_csrf,
            save_file_with_mtime,
        )

        auth = request.app.state.dashboard_auth
        next_token = issue_csrf(request, key=auth.key())

        path = _skill_path(request, skill_name)
        if path is None:
            ctx = {
                "skill_name": skill_name,
                "file_path": "<invalid>",
                "csrf_token": next_token,
                "content": content,
                "expected_mtime": "",
                "error_code": "validation_failed",
                "error_detail": f"unknown skill: {skill_name!r}",
                "client": client,
            }
            return templates.TemplateResponse(request, "skills_edit.html", ctx, status_code=400)

        try:
            csrf_required(request, csrf_token)
        except CsrfMismatch:
            ctx = {
                "skill_name": skill_name,
                "file_path": str(path),
                "csrf_token": next_token,
                "content": content,
                "expected_mtime": expected_mtime,
                "error_code": "csrf_mismatch",
                "client": client,
            }
            return templates.TemplateResponse(request, "skills_edit.html", ctx, status_code=403)

        expected_ts: float | None
        if expected_mtime.strip():
            try:
                expected_ts = float(expected_mtime)
            except ValueError:
                expected_ts = None
        else:
            expected_ts = None

        from ..skills import validate_skill_content

        audit_log = getattr(request.app.state, "audit", None)

        try:
            result = save_file_with_mtime(
                path=path,
                new_content=content,
                expected_mtime=expected_ts,
                audit_log=audit_log,
                client=client,
                validate=validate_skill_content,
            )
        except MtimeConflict as exc:
            ctx = {
                "skill_name": skill_name,
                "file_path": str(path),
                "csrf_token": next_token,
                "content": content,
                "expected_mtime": expected_ts or "",
                "error_code": "mtime_conflict",
                "current_mtime": exc.current_mtime,
                "client": client,
            }
            return templates.TemplateResponse(request, "skills_edit.html", ctx, status_code=409)
        except ValidationFailed as exc:
            ctx = {
                "skill_name": skill_name,
                "file_path": str(path),
                "csrf_token": next_token,
                "content": content,
                "expected_mtime": expected_ts or "",
                "error_code": "validation_failed",
                "error_detail": exc.detail,
                "client": client,
            }
            return templates.TemplateResponse(request, "skills_edit.html", ctx, status_code=400)
        except Exception:
            ctx = {
                "skill_name": skill_name,
                "file_path": str(path),
                "csrf_token": next_token,
                "content": content,
                "expected_mtime": expected_ts or "",
                "error_code": "io_error",
                "client": client,
            }
            return templates.TemplateResponse(request, "skills_edit.html", ctx, status_code=500)

        ctx = {
            "skill_name": skill_name,
            "file_path": str(path),
            "csrf_token": next_token,
            "content": content,
            "expected_mtime": result.new_mtime,
            "saved_at": time.time(),
            "bytes_written": result.bytes_written,
            "client": client,
        }
        return templates.TemplateResponse(request, "skills_edit.html", ctx)

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

    # --------------------------------------------------------- typed actions (F16)

    @router.post("/actions/refresh-aliases", response_class=HTMLResponse)
    async def refresh_aliases_action(
        request: Request,
        csrf_token: str = Form(""),
    ) -> Response:
        return await _run_typed_action(
            request,
            csrf_token=csrf_token,
            action_name="refresh_aliases",
            action_args={},
            redirect_target="/dashboard/aliases",
            run=lambda: _action_refresh_aliases(request),
        )

    @router.post("/actions/reprobe-aliases", response_class=HTMLResponse)
    async def reprobe_aliases_action(
        request: Request,
        csrf_token: str = Form(""),
    ) -> Response:
        return await _run_typed_action(
            request,
            csrf_token=csrf_token,
            action_name="reprobe_aliases",
            action_args={},
            redirect_target="/dashboard/aliases",
            run=lambda: _action_reprobe_aliases(request),
        )

    @router.post("/actions/reprobe-alias", response_class=HTMLResponse)
    async def reprobe_alias_action(
        request: Request,
        csrf_token: str = Form(""),
        alias: str = Form(""),
    ) -> Response:
        """Re-probe ONE alias (Phase 7.6 Decision 4). Redirects
        back to that alias's page so the operator sees the fresh
        result in context."""
        cleaned = alias.strip()
        target = f"/dashboard/alias/{cleaned}" if cleaned else "/dashboard/aliases"
        return await _run_typed_action(
            request,
            csrf_token=csrf_token,
            action_name="reprobe_alias",
            action_args={"alias": cleaned},
            redirect_target=target,
            run=lambda: _action_reprobe_alias(request, alias=cleaned),
        )

    @router.post("/actions/mcp-restart", response_class=HTMLResponse)
    async def mcp_restart_action(
        request: Request,
        csrf_token: str = Form(""),
        name: str = Form(""),
    ) -> Response:
        cleaned = name.strip()
        return await _run_typed_action(
            request,
            csrf_token=csrf_token,
            action_name="mcp_restart",
            action_args={"name": cleaned},
            redirect_target="/dashboard/health",
            run=lambda: _action_mcp_restart(request, name=cleaned),
        )

    @router.post("/actions/audit-verify", response_class=HTMLResponse)
    async def audit_verify_action(
        request: Request,
        csrf_token: str = Form(""),
    ) -> Response:
        return await _run_typed_action(
            request,
            csrf_token=csrf_token,
            action_name="audit_verify",
            action_args={},
            redirect_target="/dashboard/audit",
            run=lambda: _action_audit_verify(request),
        )

    @router.post("/actions/pruner-tick", response_class=HTMLResponse)
    async def pruner_tick_action(
        request: Request,
        csrf_token: str = Form(""),
        which: str = Form(""),
    ) -> Response:
        cleaned = which.strip()
        return await _run_typed_action(
            request,
            csrf_token=csrf_token,
            action_name="pruner_tick",
            action_args={"which": cleaned},
            redirect_target="/dashboard/health",
            run=lambda: _action_pruner_tick(request, which=cleaned),
        )

    @router.post("/actions/run-eval", response_class=HTMLResponse)
    async def run_eval_action(
        request: Request,
        csrf_token: str = Form(""),
        alias: str = Form(""),
        suite: str = Form("default"),
        redirect_to: str = Form(""),
    ) -> Response:
        cleaned = alias.strip()
        cleaned_suite = suite.strip() or "default"
        # The aliases table posts without redirect_to (stays on
        # /dashboard/aliases); the per-alias page posts
        # redirect_to=alias so the operator returns to the page
        # they ran it from. Only same-origin dashboard paths are
        # honoured.
        target = "/dashboard/aliases"
        if redirect_to == "alias" and cleaned:
            target = f"/dashboard/alias/{cleaned}#eval"
        return await _run_typed_action(
            request,
            csrf_token=csrf_token,
            action_name="run_eval",
            action_args={"alias": cleaned, "suite": cleaned_suite},
            redirect_target=target,
            run=lambda: _action_run_eval(request, alias=cleaned, suite=cleaned_suite),
        )

    @router.post("/actions/profile-alias", response_class=HTMLResponse)
    async def profile_alias_action(
        request: Request,
        csrf_token: str = Form(""),
        alias: str = Form(""),
    ) -> Response:
        """Build the capability profile for ONE alias (Phase 12.5a).
        Redirects back to that alias's page so the operator sees the
        fresh Capability card in context."""
        cleaned = alias.strip()
        target = f"/dashboard/alias/{cleaned}" if cleaned else "/dashboard/aliases"
        return await _run_typed_action(
            request,
            csrf_token=csrf_token,
            action_name="profile_alias",
            action_args={"alias": cleaned},
            redirect_target=target,
            run=lambda: _action_profile_alias(request, alias=cleaned),
        )

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
