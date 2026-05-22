"""GET /v1/aliases — FITT-internal per-alias detail (Phase 7).

The existing ``/v1/models`` endpoint serves the OpenAI-shape
``/v1/models`` contract — clients (Continue, Cursor, Open
WebUI) ping it for the alias list as part of normal model-
picker UX. It's auth-exempt and returns minimal per-alias
fields plus FITT extensions (``fitt_backend``,
``fitt_resolved_model``, ``fitt_fallback``).

``/v1/aliases`` is the operator-facing detail surface. Bearer-
gated. Returns the same per-alias detail plus everything Phase
7's visibility surfaces want to render:

* Discovered context window (Slice 7.1) — tokens + provenance.
* Last boot-probe result — status, detail, when it ran.
* Last eval report — pass rate, when it ran.

Read by the Telegram ``/model`` command (extended in Slice 7.3),
the dashboard's aliases view (Slice 7.5), and any future tool
that wants the same picture without reading on-disk state.

Schema: see ``.kiro/specs/phase7-visibility-traceability/design.md``.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from .alias_eval import default_eval_dir
from .config import fitt_home

router = APIRouter()
_log = logging.getLogger(__name__)


# --------------------------------------------------------------- eval parsing


# Match the ``- Result: **N/M passed** (XX%)`` line written by
# ``alias_eval.render_report_markdown``. The format is stable —
# changing it requires updating this regex too.
_EVAL_RESULT_RE = re.compile(
    r"^-\s+Result:\s+\*\*(?P<passed>\d+)/(?P<total>\d+)\s+passed\*\*\s+\((?P<pct>\d+)%\)"
)
_EVAL_FINISHED_RE = re.compile(r"^-\s+Finished:\s+(?P<iso>\S+)")


def _parse_latest_eval(path: Path) -> dict[str, Any] | None:
    """Parse a rolling eval report's header for pass rate +
    finished timestamp. Returns ``None`` when the file's
    missing or unparseable — surfaces as ``last_eval: null`` to
    the operator rather than failing the endpoint."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning(
            "aliases.eval_read_failed",
            extra={"path": str(path), "error": f"{type(exc).__name__}: {exc}"},
        )
        return None

    passed: int | None = None
    total: int | None = None
    finished_iso: str | None = None
    for line in text.splitlines()[:30]:  # only the header
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

    pass_rate = passed / total if total > 0 else 0.0
    out: dict[str, Any] = {
        "passed": passed,
        "total": total,
        "pass_rate": pass_rate,
    }
    if finished_iso is not None:
        out["finished_at"] = finished_iso
    return out


# --------------------------------------------------------------- endpoint


@router.get("/v1/aliases")
async def list_aliases(request: Request) -> dict[str, Any]:
    config = request.app.state.config
    cache = getattr(request.app.state, "context_windows", None)
    probe_results: dict[str, Any] = getattr(request.app.state, "alias_probe_results", {}) or {}
    probe_ran_at: float | None = getattr(request.app.state, "alias_probe_ran_at", None)

    eval_dir = default_eval_dir(fitt_home())

    aliases: list[dict[str, Any]] = []
    for alias in config.alias_names():
        chain = config.resolve_alias(alias)
        primary = chain[0]
        fallback = chain[1] if len(chain) > 1 else None

        # Context window — present iff cache is wired and the
        # binding's discovery completed.
        cw_payload: dict[str, Any] | None = None
        if cache is not None:
            cw = cache.get(primary.backend, primary.id)
            if cw is not None:
                cw_payload = {
                    "tokens": cw.tokens,
                    "source": cw.source,
                    "detail": cw.detail,
                    "discovered_at": cw.discovered_at,
                }

        # Last probe — only present if the probe ran for this
        # alias. Probes are skipped for missing api keys
        # (api_keys check already logged it).
        probe_payload: dict[str, Any] | None = None
        probe = probe_results.get(alias)
        if probe is not None and probe_ran_at is not None:
            probe_payload = {
                "status": probe.status,
                "detail": probe.detail,
                "model_used": probe.model_used,
                "ran_at": probe_ran_at,
            }

        # Last eval report — read the rolling per-alias file.
        eval_path = eval_dir / f"{alias}-latest.md"
        eval_payload = _parse_latest_eval(eval_path)

        entry: dict[str, Any] = {
            "id": alias,
            "primary": {
                "model_id": primary.id,
                "model": primary.model,
                "backend": primary.backend,
                "endpoint": primary.endpoint,
            },
            "fallback": (
                {
                    "model_id": fallback.id,
                    "model": fallback.model,
                    "backend": fallback.backend,
                }
                if fallback is not None
                else None
            ),
            "context_window": cw_payload,
            "last_probe": probe_payload,
            "last_eval": eval_payload,
        }
        aliases.append(entry)

    return {
        "aliases": aliases,
        "generated_at": time.time(),
    }


@router.post("/v1/internal/context-refresh")
async def context_refresh(request: Request) -> dict[str, Any]:
    """Re-run context-window discovery.

    Body: ``{}`` for all bindings, or ``{"model_id": "..."}`` for one.
    Used by ``fitt context refresh`` when the operator has changed a
    backend's config (e.g. raised Ollama's ``OLLAMA_CONTEXT_LENGTH``)
    and wants the gateway to pick it up without a process restart.

    Internal: bearer-gated, FITT-CLI-only by intent (the bot doesn't
    use it; the dashboard might land an "edit and refresh" follow-up
    but doesn't today). Returns the refreshed result(s) so the CLI
    can render them inline."""
    config = request.app.state.config
    cache = getattr(request.app.state, "context_windows", None)
    if cache is None:
        return {"error": {"type": "unavailable", "message": "context cache not initialised"}}

    try:
        body = await request.json()
    except Exception:
        body = {}
    model_id = body.get("model_id") if isinstance(body, dict) else None
    timeout_s = float(getattr(config.server, "context_probe_timeout_s", 5.0))

    if model_id:
        try:
            result = await cache.refresh_one(config, model_id, timeout_s=timeout_s)
        except KeyError:
            return {
                "error": {
                    "type": "unknown_model",
                    "message": f"no model with id {model_id!r} in config",
                }
            }
        return {
            "refreshed": [
                {
                    "model_id": model_id,
                    "tokens": result.tokens,
                    "source": result.source,
                    "detail": result.detail,
                    "discovered_at": result.discovered_at,
                }
            ]
        }

    # No model_id: refresh everything.
    await cache.populate(config, timeout_s=timeout_s)
    refreshed: list[dict[str, Any]] = []
    for (backend, mid), result in cache.all_results().items():
        refreshed.append(
            {
                "model_id": mid,
                "backend": backend,
                "tokens": result.tokens,
                "source": result.source,
                "detail": result.detail,
                "discovered_at": result.discovered_at,
            }
        )
    return {"refreshed": refreshed}
