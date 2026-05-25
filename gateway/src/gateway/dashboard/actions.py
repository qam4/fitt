"""Dashboard typed-action helpers (F12 + F16 substrate).

Each editable surface that doesn't fit the "edit this markdown
file" shape (F11) instead exposes a few **typed POST endpoints**
that call into existing gateway code paths. The shape:

1. Route handler does CSRF-required first.
2. Route handler calls the underlying registry / service
   method.
3. We emit one audit entry summarising the action — same
   chain F10 + F11 use, different ``tool`` value.

This module owns the audit emission so every action endpoint
has a one-line entry into the audit log without re-implementing
the bookkeeping. Tools we expose via this path:

* ``dashboard.project_add`` / ``dashboard.project_update`` /
  ``dashboard.project_remove`` (F12)
* ``dashboard.cron_update`` / ``dashboard.cron_remove`` (F12)
* Future: ``dashboard.action.<name>`` for the typed buttons in
  F16 (Refresh aliases, Restart MCP, Verify audit, Run eval).

The action helpers are deliberately thin wrappers — no business
logic. The registry / service is still the source of truth. This
module makes the dashboard call them in a uniform way.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from ..audit import AuditLog, new_entry

_log = logging.getLogger(__name__)


def audit_action(
    audit_log: AuditLog | None,
    *,
    tool: str,
    args: dict[str, Any],
    client: str,
    session_key: str = "main",
    ok: bool,
    decision: str,
    error: str = "",
    duration_ms: int = 0,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one audit entry for a dashboard action.

    The ``tool`` name follows the ``dashboard.<verb>`` namespace
    so an ``audit tail --tool 'dashboard.*'`` filter matches
    every dashboard-driven mutation. Failures (validation,
    KeyError, IO) audit too with ``ok=False``; the audit log
    captures the *attempt* regardless of outcome, which is
    exactly the contract the rest of the audit chain follows.

    Audit failure must not break the action. Same posture as
    :func:`gateway.dashboard.edit._audit_edit`: a broken audit
    log gets logged at WARNING; the on-disk action is the
    source of truth.
    """
    if audit_log is None:
        return
    payload_extra: dict[str, Any] = dict(extra or {})
    try:
        audit_log.append(
            new_entry(
                session_key=session_key,
                client=client,
                tool=tool,
                args=args,
                decision=decision,
                ok=ok,
                duration_ms=duration_ms,
                error=error,
                extra=payload_extra,
            )
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning(
            "dashboard.action_audit_failed",
            extra={
                "tool": tool,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )


class ActionTimer:
    """Tiny context manager for "wrap an action, time it, audit
    it" patterns. Used inline by the route handlers; saves
    boilerplate when the success path is one method call and
    the failure paths each map to one exception type.

    Usage::

        with ActionTimer() as t:
            registry.add(project)
        audit_action(..., duration_ms=t.elapsed_ms, ...)

    Not strictly required — manual timing works — but keeps
    the route handlers readable when there are five of them.
    """

    def __init__(self) -> None:
        self._start = 0.0
        self.elapsed_ms = 0

    def __enter__(self) -> ActionTimer:
        self._start = time.time()
        return self

    def __exit__(self, *_: object) -> None:
        self.elapsed_ms = int((time.time() - self._start) * 1000)


def safe_str(value: Any) -> str:
    """Best-effort string for audit ``args`` payloads.
    Avoids dragging in the full :mod:`gateway.audit.redact`
    chain for one field — the audit appender redacts already
    when it serialises."""
    if value is None:
        return ""
    return str(value)


def relative_path(path: Path, *, base: Path | None = None) -> str:
    """Render a path relative to FITT_HOME if possible, else
    absolute. Used for audit ``args.path`` so chain entries
    stay portable across hub / dev environments."""
    try:
        if base is not None:
            return str(path.relative_to(base))
    except ValueError:
        pass
    return str(path)
