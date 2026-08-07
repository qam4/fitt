"""Judged end-to-end harness — driver pieces (Phase C).

The side-effect **snapshot** (this file, first) reads the real end state
the objective assertions check — cron jobs, the todo list, recent
events — into a plain dict that rides in the trajectory as ground
truth. The **dispatch** (turns → real chat pipeline → reply +
tool_sequence) and the ``fitt eval e2e`` CLI wiring build on top.

Kept dependency-light and app-driven so `snapshot_app` is testable
against an in-process gateway with a seeded store, no live model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import fitt_home


def snapshot_app(app: Any, *, session_id: str = "main", event_tail: int = 20) -> dict[str, Any]:
    """Read the relevant stores at run end into a JSON-able dict.

    This is the ground truth an ``OutcomeAssert`` inspects: did a cron
    get created for the right time, did the todo land, what fired. Never
    raises — a missing/failed store just yields an empty slice."""
    snap: dict[str, Any] = {}

    # Cron store — the reminder scenario asserts on this.
    cron = getattr(app.state, "cron", None)
    if cron is not None:
        try:
            cron.reload_if_changed()
            snap["cron_jobs"] = [
                {
                    "id": j.id,
                    "name": j.name,
                    "message": j.message,
                    "schedule_kind": j.schedule.kind,
                    "at_ts": j.schedule.at_ts,
                    "enabled": j.enabled,
                }
                for j in cron.list(include_disabled=True)
            ]
        except Exception as exc:  # pragma: no cover - defensive
            snap["cron_error"] = str(exc)

    # Todo list (Phase E) — read the markdown if present.
    todos_path = fitt_home() / "todos.md"
    try:
        snap["todos_text"] = todos_path.read_text(encoding="utf-8") if todos_path.exists() else ""
    except OSError:
        snap["todos_text"] = ""

    # Recent event kinds — coarse "what happened" signal.
    events = getattr(app.state, "events", None)
    if events is not None:
        try:
            recent = events.read(limit=event_tail)
            snap["event_kinds"] = [getattr(e, "kind", None) for e in recent]
        except Exception:  # pragma: no cover - defensive
            snap["event_kinds"] = []

    return snap


def cron_at_ts_matches(
    snap: dict[str, Any], target_ts: float, *, tolerance_s: float = 900.0
) -> bool:
    """Helper for the reminder assertion: is there a one-shot (`at`) cron
    whose fire time is within ``tolerance_s`` of ``target_ts``?"""
    for job in snap.get("cron_jobs", []):
        if job.get("schedule_kind") == "at" and job.get("at_ts") is not None:
            if abs(float(job["at_ts"]) - target_ts) <= tolerance_s:
                return True
    return False


def todos_contain(snap: dict[str, Any], substring: str) -> bool:
    """Helper for the todo assertion: did the todo list gain the item?"""
    return substring.lower() in str(snap.get("todos_text", "")).lower()


def _todos_path() -> Path:
    return fitt_home() / "todos.md"
