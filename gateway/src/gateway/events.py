"""Phase 4.5 event log — user-visible async activity.

One of FITT's three append-only logs (see
``.kiro/specs/phase4.5-cron-events/design.md`` → "Three logs,
three jobs"):

- **Audit** (`audit.jsonl`): every tool call, HMAC-chained,
  tamper-evident. Security trail.
- **Capability gaps** (`capability_gaps.log`): "I'd need a tool
  to X" complaints, ranked. Product backlog.
- **Events** (`events.jsonl`, this module): user-visible async
  activity — cron fired / completed, agent_message,
  late_tool_result, approval_requested. What the operator
  scrolls through in Telegram or ``fitt inbox``.

Design choices
--------------

* **Plain JSONL, no HMAC.** Events aren't adversarial; they're
  a notification feed. Skipping the chain keeps writes cheap
  and makes the file trivially editable by hand if someone
  wants to prune it manually.
* **Append-only, flush-per-write.** ``open(path, 'a')`` is
  atomic for writes below ``PIPE_BUF`` (4 KiB on Linux), which
  covers any realistic event. Flush after each write so a
  crash loses at most the in-flight entry.
* **Delivery-independent.** The log is the source of truth.
  Telegram push and ``fitt inbox`` are both subscribers; either
  can fail without losing the record.
* **Pruning is explicit.** Callers choose when to prune (a
  daily system cron, spec task 10). Stream-rewrite to tmp then
  rename so a crash mid-prune can't leave the file truncated.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import time
from typing import Any

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class EventEntry:
    """One user-visible event.

    Fields are deliberately flat so ``json.dumps(asdict(e))``
    produces a line the operator can grep. ``meta`` holds
    kind-specific extras (cron_id, tool_name, approval_id, ...)
    that would clutter the top-level schema."""

    ts: float
    kind: str
    """What happened. Values used in Phase 4.5:

    - ``cron_fired`` / ``cron_completed`` / ``cron_failed``
    - ``approval_requested`` / ``approval_resolved``
    - ``late_tool_result`` / ``late_tool_rejected`` — the
      detached-delivery path closing the Phase 4 rough edge.
    - ``agent_message`` — explicit ``send_message`` tool call.
    - ``system_pruned`` — the pruner itself emits one of these
      after each run so ``fitt inbox`` shows there wasn't a
      gap.
    - ``tool_executed`` — Phase 4.7; emitted by
      ``project_shell`` after dispatch (regardless of exit
      code). Meta carries tool / project / command /
      exit_code / duration_ms / timed_out so operators can
      see the sequence of commands running in a
      trust_session flow.

    Reserved for Phase 6: ``task_started`` / ``task_completed``
    / ``task_failed``.

    New kinds earn their place when a new subsystem wants a
    coarse user-visible moment; no central registry."""

    session_key: str
    """Session the event belongs to. ``"main"`` for the default
    chat session; ``"cron:<id>:<ts>"`` for cron firings;
    ``"task:<spec>:<ts>"`` for Phase 6's task runner."""

    title: str
    """Short human-readable summary, suitable as a Telegram
    message's first line."""

    body: str = ""
    """Longer content. May be empty. Subscribers cap it at
    their own length limits (Telegram push enforces
    ``events.telegram_body_cap``)."""

    meta: dict[str, Any] = field(default_factory=dict)


class EventLog:
    """Append-only JSONL record of events.

    One instance per gateway process. ``append`` takes a
    thread-safe lock so chat-handler tasks and cron firings
    don't interleave bytes mid-line. Read-side ``read`` /
    ``prune`` also take the lock to serialise with writers."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    # -------------------------------------------------- write

    def append(self, entry: EventEntry) -> EventEntry:
        """Append ``entry`` to the log. Returns the entry
        unchanged so callers can chain an assertion in tests."""
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(asdict(entry), ensure_ascii=False) + "\n"
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
            return entry

    # -------------------------------------------------- read

    def read(
        self,
        *,
        since: float | None = None,
        kind: str | None = None,
        session: str | None = None,
        limit: int | None = None,
    ) -> list[EventEntry]:
        """Return entries matching the given filters, newest
        last (file order).

        Malformed lines are dropped with a warning. Missing file
        is an empty result — it's normal for a fresh install to
        have no log yet.

        Filters are ANDed. ``since`` is a unix timestamp;
        entries with ``ts < since`` are excluded. ``limit``
        caps the result length, keeping the *latest* entries
        (so the CLI's ``--limit 20`` gives the most recent 20,
        not the oldest)."""
        if not self._path.exists():
            return []
        out: list[EventEntry] = []
        with self._lock:
            with self._path.open("r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        _log.warning(
                            "events.malformed_line",
                            extra={"line_sample": line[:120]},
                        )
                        continue
                    try:
                        entry = EventEntry(
                            ts=float(data["ts"]),
                            kind=str(data["kind"]),
                            session_key=str(data["session_key"]),
                            title=str(data.get("title", "")),
                            body=str(data.get("body", "")),
                            meta=dict(data.get("meta", {})) if data.get("meta") else {},
                        )
                    except (KeyError, TypeError, ValueError) as e:
                        _log.warning(
                            "events.malformed_entry",
                            extra={"line_sample": line[:120], "error": str(e)},
                        )
                        continue
                    if since is not None and entry.ts < since:
                        continue
                    if kind is not None and entry.kind != kind:
                        continue
                    if session is not None and entry.session_key != session:
                        continue
                    out.append(entry)
        if limit is not None and len(out) > limit:
            # Keep the most recent `limit` entries. File order
            # is oldest-first, so truncate the head.
            out = out[-limit:]
        return out

    # -------------------------------------------------- prune

    def prune(self, *, max_age_days: int, now: float | None = None) -> int:
        """Drop entries older than ``max_age_days`` and return
        the count removed.

        Implementation: stream the existing file into a tmp
        file, writing only kept entries, then atomic-rename.
        Malformed lines are dropped (same as ``read``). A
        crash mid-prune leaves the original file untouched
        because we don't rename until we've finished writing
        the tmp."""
        if not self._path.exists():
            return 0
        cutoff = (now if now is not None else time()) - max_age_days * 86400.0
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        removed = 0
        kept = 0
        with self._lock:
            with (
                self._path.open("r", encoding="utf-8") as src,
                tmp.open("w", encoding="utf-8") as dst,
            ):
                for raw in src:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        ts = float(data["ts"])
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                        # Drop malformed entries during prune.
                        removed += 1
                        continue
                    if ts < cutoff:
                        removed += 1
                        continue
                    dst.write(raw if raw.endswith("\n") else raw + "\n")
                    kept += 1
            # Atomic replace on POSIX; on Windows ``replace``
            # overwrites the destination atomically as of Python 3.3+.
            tmp.replace(self._path)
        _log.info("events.pruned", extra={"removed": removed, "kept": kept})
        return removed


# --------------------------------------------------------------- helpers


def new_entry(
    *,
    kind: str,
    session_key: str,
    title: str,
    body: str = "",
    meta: dict[str, Any] | None = None,
    ts: float | None = None,
) -> EventEntry:
    """Convenience constructor. Stamps ``ts = time()`` unless
    provided (tests pin specific timestamps). Copies ``meta``
    so callers that pass a shared dict don't get surprised by
    mutation."""
    return EventEntry(
        ts=ts if ts is not None else time(),
        kind=kind,
        session_key=session_key,
        title=title,
        body=body,
        meta=dict(meta) if meta else {},
    )


def default_events_path(fitt_home: Path) -> Path:
    return fitt_home / "events.jsonl"
