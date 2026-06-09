"""Phase 12 task 6 — Plan model + :class:`PlanStore`.

The plan is durable, structured state (Story 1.2): an ordered list of
todo items the planner emits and the executor works against, held
*outside* the model's working context and re-injected each turn. The
representation is a markdown todo list (Story 1.4); a DAG is deferred
(Non-Goals).

**Structured round-trip is correctness property C5.** A plan
serialises to JSON and back without loss. This is the explicit fix
for the 2026-05-11 ``_persisted_args`` poisoning, where a *lossy*
prose-summary round-trip corrupted persisted state
(``docs/observed-issues.md``). We never summarise the plan to persist
it; we serialise the structure.

Recovery mirrors Hermes's ``_hydrate_todo_store``: the gateway builds
a fresh agent per turn, so the in-memory plan starts empty; we recover
it from the most recent plan-tool result in conversation history
(the tool returns a ``{"todos": [...]}`` payload).

Pure logic plus a thin optional JSON persistence layer — the
build-with-fakes half of Phase 12; no model needed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

_log = logging.getLogger(__name__)

PlanStatus = Literal["pending", "in_progress", "done", "blocked"]
PLAN_STATUSES: tuple[PlanStatus, ...] = ("pending", "in_progress", "done", "blocked")

_STATUS_MARK: dict[PlanStatus, str] = {
    "pending": "[ ]",
    "in_progress": "[~]",
    "done": "[x]",
    "blocked": "[!]",
}


# --------------------------------------------------------------- model


@dataclass(slots=True)
class PlanItem:
    """One step. ``id`` is the stable key the model and executor use to
    mark progress; ``status`` tracks completion."""

    id: str
    text: str
    status: PlanStatus = "pending"

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "text": self.text, "status": self.status}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PlanItem:
        item_id = raw.get("id")
        text = raw.get("text")
        status = raw.get("status", "pending")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError(f"plan item 'id' must be a non-empty string (got {item_id!r})")
        if not isinstance(text, str):
            raise ValueError(f"plan item 'text' must be a string (got {type(text).__name__})")
        if status not in PLAN_STATUSES:
            raise ValueError(f"plan item 'status' {status!r} not in {PLAN_STATUSES}")
        return cls(id=item_id, text=text, status=status)


@dataclass(slots=True)
class Plan:
    """An ordered list of :class:`PlanItem`. Serialises to the
    ``{"todos": [...]}`` shape the plan tool returns, so the same blob
    round-trips through a tool result, disk, and history hydration."""

    items: list[PlanItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"todos": [i.to_dict() for i in self.items]}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Plan:
        todos = raw.get("todos", [])
        if not isinstance(todos, list):
            raise ValueError(f"plan 'todos' must be a list (got {type(todos).__name__})")
        return cls(items=[PlanItem.from_dict(t) for t in todos])

    def render_markdown(self) -> str:
        """Re-injectable form: a checkbox list the model reads to stay
        oriented. Empty plan renders as a clear marker rather than an
        empty string, so the executor prompt never silently loses it."""
        if not self.items:
            return "(no plan)"
        return "\n".join(f"- {_STATUS_MARK[i.status]} {i.text}" for i in self.items)

    def mark(self, item_id: str, status: PlanStatus) -> bool:
        """Set an item's status. Returns False if no item has that id."""
        for item in self.items:
            if item.id == item_id:
                item.status = status
                return True
        return False

    def next_pending(self) -> PlanItem | None:
        for item in self.items:
            if item.status in ("pending", "in_progress"):
                return item
        return None

    def is_complete(self) -> bool:
        return bool(self.items) and all(i.status == "done" for i in self.items)


def derive_plan_from_history(history: list[dict[str, Any]]) -> Plan | None:
    """Recover the latest plan from conversation history (Hermes's
    hydrate pattern). Scans backward for the most recent ``tool``
    message whose content parses to a ``{"todos": [...]}`` payload.
    Returns ``None`` when none is found. Never raises — a malformed
    candidate is skipped, not fatal."""
    for msg in reversed(history):
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or '"todos"' not in content:
            continue
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or "todos" not in data:
            continue
        try:
            return Plan.from_dict(data)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------- store


class PlanStore:
    """Per-session plans, in memory with optional JSON persistence.

    ``path=None`` is pure in-memory (tests, ephemeral turns).
    With a path, mutations persist to a single JSON file mapping
    ``session_key -> plan`` so a fresh agent per turn (or a restart)
    can reload — durable state, not working memory (Story 1.2).
    """

    __slots__ = ("_path", "_plans")

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._plans: dict[str, Plan] = {}
        if path is not None and path.exists():
            self._load()

    def get(self, session_key: str) -> Plan | None:
        return self._plans.get(session_key)

    def set(self, session_key: str, plan: Plan) -> None:
        self._plans[session_key] = plan
        self._save()

    def clear(self, session_key: str) -> None:
        if self._plans.pop(session_key, None) is not None:
            self._save()

    def hydrate_from_history(self, session_key: str, history: list[dict[str, Any]]) -> Plan | None:
        """Populate the session's plan from history when the in-memory
        store has none (fresh-agent-per-turn). A plan already in memory
        wins (it's more current than history). Returns the effective
        plan or ``None``."""
        existing = self._plans.get(session_key)
        if existing is not None:
            return existing
        recovered = derive_plan_from_history(history)
        if recovered is not None:
            self.set(session_key, recovered)
        return recovered

    # ----------------------------------------------------------- persistence

    def _load(self) -> None:
        assert self._path is not None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning(
                "plan_store.load_failed", extra={"path": str(self._path), "error": str(exc)}
            )
            return
        if not isinstance(raw, dict):
            return
        for session_key, plan_dict in raw.items():
            if not isinstance(plan_dict, dict):
                continue
            try:
                self._plans[str(session_key)] = Plan.from_dict(plan_dict)
            except ValueError as exc:
                _log.warning(
                    "plan_store.skip_bad_plan",
                    extra={"session": session_key, "error": str(exc)},
                )

    def _save(self) -> None:
        if self._path is None:
            return
        payload = {sk: plan.to_dict() for sk, plan in self._plans.items()}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            _log.warning(
                "plan_store.save_failed", extra={"path": str(self._path), "error": str(exc)}
            )


__all__ = [
    "PLAN_STATUSES",
    "Plan",
    "PlanItem",
    "PlanStatus",
    "PlanStore",
    "derive_plan_from_history",
]
