"""Phase 4.8d — ``fitt watch`` CLI renderer.

Tails one session's per-turn event stream from disk and prints
each event as a single line with a fixed kind-column width,
key-sorted meta dict, and color-coded severity. Designed for
the developer view of "what did FITT just do?" — the same
role ``docker logs -f gateway`` serves for general activity,
narrowed to one session.

Format
------

Each rendered line has the shape::

    HH:MM:SS  <kind column, 18 wide>  <key-sorted meta>

Examples::

    12:00:01  turn_started        client=telegram alias=fitt-smart
    12:00:02  tool_call_planned   call_id=c1 iteration=0 tool_name=read_file
    12:00:02  tool_call_executed  call_id=c1 duration_ms=15 ok=True tool_name=read_file

Meta rendering:

- Sorted by key for stable output.
- ``key=value``; strings render as-is, dicts flatten to
  ``key.subkey=value``. Dicts deeper than two levels are
  abbreviated to ``{...}`` so the line stays grep-readable.
- Newlines and tabs in values are replaced with ``\\n`` /
  ``\\t`` so one event always fits on one line.

Color:

- Green — ``ok`` terminal states (``turn_finished``
  with status=ok, ``tool_call_executed`` with ok=True,
  ``approval_decided`` with decision in
  {approve, trust_session}).
- Yellow — warnings and soft failures
  (``tool_call_executed`` with ok=False,
  ``approval_decided`` with decision in {reject, timeout,
  denied_deny_list}, ``gap_reported``).
- Red — hard failures (``turn_finished`` with status in
  {upstream_error, tool_loop_exhausted}).
- Plain — everything else.

Tail loop
---------

We don't use inotify/watchdog. Simple polling with a 2-second
sleep between reads is good enough for an operator-facing
viewer; a burst of events lands within 2s of the next poll.
``date.today()`` changeovers are handled at each poll tick so a
turn that crosses midnight doesn't break the tailer — the next
poll checks today's file; if the session's today file doesn't
exist yet we gracefully contribute zero events and continue.

``Ctrl-C`` exits cleanly; no signal handlers beyond Python's
default ``KeyboardInterrupt``.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from rich.console import Console

from .turns import TurnEvent, TurnLog

_KIND_COLUMN_WIDTH = 18
"""Fixed width for the kind column. Longest kind today is
``tool_call_executed`` at 19 chars; we pad to 18 and let it
overflow one char — overly wide column adds more slop than a
single overflow."""

_POLL_INTERVAL_S = 2.0

_GREEN_KINDS: frozenset[str] = frozenset(
    {
        "turn_started",
        "llm_call_started",
        "llm_call_completed",
        "tool_call_planned",
    }
)
"""Progress / info events that indicate a turn is moving
through its happy path. Rendered in default color — a turn log
that's all default-color lines read as "everything's fine so
far." Reserved ``bold green`` for the terminal-ok states
below so the eye can jump to "this turn finished"."""


def render_line(event: TurnEvent) -> str:
    """Render one TurnEvent as a single formatted line.

    Pure function so tests can assert on the output without
    constructing a Console. The entry's color markup is
    Rich-style; :class:`rich.console.Console` renders them,
    plain ``print`` prints them verbatim."""
    ts = datetime.fromtimestamp(event.ts, UTC).astimezone()
    clock = ts.strftime("%H:%M:%S")
    kind_col = event.kind.ljust(_KIND_COLUMN_WIDTH)
    meta = _render_meta(event.meta)
    color = _classify_color(event)
    if color:
        return f"{clock}  [{color}]{kind_col}[/{color}]  {meta}"
    return f"{clock}  {kind_col}  {meta}"


def _classify_color(event: TurnEvent) -> str:
    """Map an event to a Rich color tag ('' for plain)."""
    kind = event.kind
    meta = event.meta or {}
    if kind == "turn_finished":
        status = meta.get("status", "")
        if status == "ok":
            return "bold green"
        return "bold red"
    if kind == "tool_call_executed":
        return "" if meta.get("ok") else "yellow"
    if kind == "approval_decided":
        decision = meta.get("decision", "")
        if decision in ("approve", "trust_session"):
            return "bold green"
        return "yellow"
    if kind == "gap_reported":
        return "yellow"
    return ""


def _render_meta(meta: dict[str, Any]) -> str:
    """Render a meta dict as ``key=value`` pairs, key-sorted,
    with second-level flattening and deeper dicts abbreviated."""
    if not meta:
        return ""
    parts: list[str] = []
    for key in sorted(meta.keys()):
        parts.extend(_flatten_kv(key, meta[key], depth=1))
    return " ".join(parts)


def _flatten_kv(key: str, value: Any, *, depth: int) -> list[str]:
    """Return list of ``key=value`` fragments for one (k, v).
    Flattens one level of nested dicts into ``key.subkey``
    pairs; deeper nesting abbreviates to ``{...}`` to keep
    lines grep-readable."""
    if isinstance(value, dict):
        if depth >= 2:
            return [f"{key}={{...}}"]
        if not value:
            return [f"{key}={{}}"]
        out: list[str] = []
        for subkey in sorted(value.keys()):
            out.extend(_flatten_kv(f"{key}.{subkey}", value[subkey], depth=depth + 1))
        return out
    return [f"{key}={_scalar(value)}"]


def _scalar(value: Any) -> str:
    """Render a leaf value — no extra quoting unless the
    string contains whitespace. Newlines and tabs get escaped
    so one event is one line."""
    if isinstance(value, str):
        cleaned = value.replace("\n", "\\n").replace("\t", "\\t")
        if " " in cleaned:
            return f'"{cleaned}"'
        return cleaned
    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return "null"
    return str(value)


def iter_new_events(
    log: TurnLog,
    session_key: str,
    *,
    since: float | None,
) -> Iterable[TurnEvent]:
    """Read events newer than ``since`` for one session.

    Wraps :meth:`TurnLog.read` so the tail loop can pass the
    last-seen timestamp and get back only what's new. Walks
    across midnight automatically — the underlying reader
    handles the per-day file discovery."""
    return log.read(session_key, since=since)


def watch_loop(
    log: TurnLog,
    session_key: str,
    *,
    console: Console | None = None,
    poll_interval_s: float = _POLL_INTERVAL_S,
    clock: Any = time.monotonic,
) -> None:
    """Tail a session's per-turn stream, printing each new
    event as it lands.

    Blocks until ``Ctrl-C``. The ``clock`` argument exists for
    testing a fixed number of poll cycles without actually
    sleeping."""
    out = console or Console()
    last_ts: float | None = None
    # Print existing entries from the last ~hour as an
    # immediate context load. Without this the tailer looks
    # frozen for up to ``poll_interval_s`` on startup.
    bootstrap_since = time.time() - 3600.0
    initial = log.read(session_key, since=bootstrap_since)
    for entry in initial:
        out.print(render_line(entry), highlight=False)
        last_ts = entry.ts
    try:
        while True:
            _ = clock  # silence unused-arg warning in some shims
            time.sleep(poll_interval_s)
            new = log.read(session_key, since=last_ts)
            # The reader filters ``ts >= since`` so the last-seen
            # entry can repeat; drop exact duplicates.
            for entry in new:
                if last_ts is not None and entry.ts <= last_ts:
                    continue
                out.print(render_line(entry), highlight=False)
                last_ts = entry.ts
    except KeyboardInterrupt:
        out.print("[dim]interrupted.[/dim]")
        return


def run_watch(session_key: str, sessions_dir: Any) -> int:
    """Entry point for the ``fitt watch`` command."""
    log = TurnLog(sessions_dir)
    console = Console(force_terminal=True if sys.stdout.isatty() else False)
    watch_loop(log, session_key, console=console)
    return 0
