"""Phase 4.8 per-turn event stream.

One of the four FITT append-only logs; sibling to
:mod:`gateway.events` with a different responsibility.

Three logs already documented:

- **Audit** (`audit.jsonl`): every tool call, HMAC-chained,
  tamper-evident. Security trail.
- **Events** (`events.jsonl`): coarse user-visible async
  activity — cron_fired, agent_message, tool_executed. What
  ``fitt inbox`` and the Telegram push channel show.
- **Capability gaps** (`capability_gaps.log`): "I'd need a tool
  to X" complaints, ranked.

Phase 4.8 adds a fourth:

- **Turns** (`sessions/<k>/turns/<YYYY-MM-DD>.jsonl`,
  **this module**): fine-grained per-turn detail — every LLM
  dispatch, every tool call planned / executed, every
  approval requested / decided. Readers (`fitt watch`, the
  per-turn HTTP endpoint, the HTML viewer, the future admin
  dashboard) render these to answer "what actually happened
  in that turn?".

Relationship to `events.jsonl`
------------------------------

`events.jsonl` is per-hub and user-facing. One entry per
notable coarse moment (a cron fired, an approval landed, a
tool produced output). Events survive for 90 days and feed
the Telegram push channel — operators read them passively.

`turns/*.jsonl` is per-session and developer-facing. Many
entries per turn — every LLM call, every tool plan, every
iteration, every finish. Turns stay on disk for 90 days via
the same pruner but nobody's subscribing — they get read on
demand by `fitt watch` / the HTTP endpoint when something
feels off.

The two logs intentionally overlap a little. A
`tool_executed` lands in both: `events.jsonl` gets the
user-visible "shell ran `git pull`, exit 0" entry; `turns`
gets the same fact but richer (duration, call id, tool args,
artifact path if hoisted). No code actually duplicates
between them — the event log's entry comes from
`gateway.tools.project_shell`'s finishing hook; the turn
log's comes from `run_agent_loop`'s per-iteration emission.
Different layers, same underlying fact.

File layout
-----------

::

    $FITT_HOME/sessions/<session_key>/turns/<YYYY-MM-DD>.jsonl

Per-session, per-day. Matches the history file layout
(`sessions/<k>/history/<YYYY-MM-DD>.md`) so the existing
history pruner walks both with the same date-parsing loop —
no new retention knob.

A turn that crosses midnight writes later events to the next
day's file. In practice chat turns finish in seconds and this
never happens; for the rare case (cron firing at 23:59:58 →
tool call completing at 00:00:02), readers that want the full
turn stitch the two adjacent days by filtering on `turn_id`.

Schema
------

Every line is a JSON object with:

- `turn_id`: uuid4, constant across all events in one turn.
- `event_id`: uuid4, unique per event.
- `kind`: string. See :data:`TURN_EVENT_KINDS` for the set.
- `ts`: unix timestamp (float).
- `session_key`: the session the turn belongs to.
- `meta`: kind-specific dict.

Schema is additive per property P6 — callers may encounter
unknown `kind` values or unknown `meta` keys in older logs
and must ignore them gracefully. Don't tighten a JSON
schema here.

Correctness
-----------

Matches the design.md properties (P1-P7):

- One line per append. One `write()` call, flushed. No
  partial lines.
- Ordered within a turn. Single writer per turn (the chat
  loop or cron runner for that request), sequential append.
- IO failure is non-fatal. A full disk or a permissions
  error logs a warning and the turn continues — lost
  visibility is worse than a stopped FITT.
- Concurrent sessions don't collide because the path
  includes the session key.

Concurrent writes to the same file can happen only when two
turns within the same session race (e.g. a user message
lands while a cron firing is mid-flight). The
`threading.Lock` makes those writes atomic line-by-line,
mirroring the `events.jsonl` posture. We don't use a
filesystem lock because the write is < 4 KiB which is atomic
on POSIX for `O_APPEND` writes; Windows honours the in-proc
lock which is what we actually care about since the gateway
runs as one process.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import time
from typing import Any

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- event kinds


TURN_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "turn_started",
        "llm_call_started",
        "llm_call_completed",
        "tool_call_planned",
        "approval_requested",
        "approval_decided",
        "tool_call_executed",
        "gap_reported",
        "turn_finished",
    }
)
"""The canonical set. New kinds get added here when the
producer lands; readers treat unknown kinds as opaque
"something happened" entries rather than error cases. Kept
as a module constant so tests can import it to exhaustively
match a writer's emissions, and so future spec revisions
have a single place to update."""


# --------------------------------------------------------------- entry


@dataclass(slots=True)
class TurnEvent:
    """One entry in `turns/<date>.jsonl`.

    Fields chosen flat for the same reason as
    :class:`gateway.events.EventEntry`: `json.dumps(asdict(e))`
    produces a line the operator can grep without dealing with
    nested structure.

    `turn_id` and `event_id` are strings (UUIDs) rather than
    integers because (a) they need to be unique across gateway
    restarts without coordination, (b) UUIDs don't impose any
    order semantics the way a sequence number would, and (c)
    readers that want to join events across day-boundary
    splits do so by `turn_id` comparison without needing a
    central counter."""

    turn_id: str
    """Constant within a turn, unique across turns. Generated
    once per chat request in :func:`gateway.chat` and once per
    cron firing in :class:`gateway.cron_runner.CronRunner`."""

    event_id: str
    """Unique per event. Primarily useful for idempotent
    reconstruction and for debugging duplicates ("did this
    `approval_requested` event land twice?")."""

    kind: str
    """See :data:`TURN_EVENT_KINDS`."""

    ts: float
    """Unix timestamp. Not ISO 8601 because we want cheap
    `ts < cutoff` comparisons in the pruner and the reader's
    `since` filter — consistent with `events.jsonl`."""

    session_key: str
    """Session the turn belongs to. Redundant with the file
    path (the file lives under `sessions/<key>/turns/`) but
    carried on the entry so a reader that concatenates logs
    across sessions (future admin dashboard) doesn't lose
    provenance."""

    meta: dict[str, Any] = field(default_factory=dict)
    """Kind-specific fields. See design.md for the per-kind
    schema. Additive — new keys may appear for an existing
    `kind` over time; readers must ignore unknowns."""


# --------------------------------------------------------------- log


class TurnLog:
    """Append-only JSONL store over per-session-per-day files.

    One instance per gateway process. The in-process
    :class:`threading.Lock` serialises writes so concurrent
    turns in the same session don't interleave lines.

    Deliberately not async. Writes are < 4 KiB and happen at
    most ~10-20 times per turn; the blocking cost is
    negligible compared to an LLM dispatch and it keeps the
    implementation small. If profiling ever shows this matters
    we can wrap `append` in `run_in_executor`.
    """

    def __init__(self, sessions_dir: Path) -> None:
        self._sessions_dir = sessions_dir
        self._lock = threading.Lock()

    # ------------------------------------------------ path helpers

    def file_path(self, session_key: str, day: date) -> Path:
        """Resolve the on-disk path for one session-day.

        Public helper so the CLI's `fitt watch` can stat the
        expected path at tail time without duplicating the
        layout logic."""
        return self._sessions_dir / session_key / "turns" / f"{day.isoformat()}.jsonl"

    # ------------------------------------------------ write

    def append(self, entry: TurnEvent) -> TurnEvent:
        """Append `entry` to the turn log for its session.

        IO failures are non-fatal per design.md P3 — we log a
        warning and return the entry unchanged so the caller
        can proceed without an exception. Lost visibility is
        worse than a stopped FITT.

        Returns the entry so test helpers can chain assertions
        on the appended value, mirroring
        :meth:`gateway.events.EventLog.append`."""
        day = datetime.fromtimestamp(entry.ts, tz=UTC).date()
        path = self.file_path(entry.session_key, day)
        with self._lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                line = json.dumps(asdict(entry), ensure_ascii=False) + "\n"
                with path.open("a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
            except OSError as exc:
                _log.warning(
                    "turns.append_failed",
                    extra={
                        "path": str(path),
                        "kind": entry.kind,
                        "session_key": entry.session_key,
                        "error": str(exc),
                    },
                )
        return entry

    # ------------------------------------------------ read

    def read(
        self,
        session_key: str,
        *,
        since: float | None = None,
        kind: str | None = None,
        turn_id: str | None = None,
        limit: int | None = None,
        now: float | None = None,
    ) -> list[TurnEvent]:
        """Return entries for `session_key`, oldest first.

        Walks the day files from the day containing `since` up
        to today. When `since` is `None` we read the last 30
        days of history — bounded to keep an unused session
        from dragging its full 90-day retention every call.
        Callers that want more can pass an explicit `since`.

        Filters (`kind`, `turn_id`) are applied after parsing.
        `limit` caps the result to the most recent N entries
        (file order = chronological, so we slice from the end).

        Malformed lines log a warning and get dropped, mirroring
        the `events.jsonl` reader. A missing day file is a
        silent empty contribution — a fresh session has no
        files yet.
        """
        now_ts = now if now is not None else time()
        today = datetime.fromtimestamp(now_ts, tz=UTC).date()
        if since is None:
            start_day = today - timedelta(days=30)
        else:
            start_day = datetime.fromtimestamp(since, tz=UTC).date()

        out: list[TurnEvent] = []
        with self._lock:
            day = start_day
            while day <= today:
                path = self.file_path(session_key, day)
                if path.exists():
                    out.extend(self._read_one_file(path, since=since))
                day += timedelta(days=1)

        if kind is not None:
            out = [e for e in out if e.kind == kind]
        if turn_id is not None:
            out = [e for e in out if e.turn_id == turn_id]
        if limit is not None and len(out) > limit:
            out = out[-limit:]
        return out

    def _read_one_file(self, path: Path, *, since: float | None) -> list[TurnEvent]:
        """Parse one day file. Split out for testability; each
        file is independent so a corrupt line in one day doesn't
        take out the sweep across days."""
        out: list[TurnEvent] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        _log.warning(
                            "turns.malformed_line",
                            extra={
                                "path": str(path),
                                "line_sample": line[:120],
                            },
                        )
                        continue
                    try:
                        entry = TurnEvent(
                            turn_id=str(data["turn_id"]),
                            event_id=str(data["event_id"]),
                            kind=str(data["kind"]),
                            ts=float(data["ts"]),
                            session_key=str(data["session_key"]),
                            meta=(dict(data.get("meta", {})) if data.get("meta") else {}),
                        )
                    except (KeyError, TypeError, ValueError) as exc:
                        _log.warning(
                            "turns.malformed_entry",
                            extra={
                                "path": str(path),
                                "line_sample": line[:120],
                                "error": str(exc),
                            },
                        )
                        continue
                    if since is not None and entry.ts < since:
                        continue
                    out.append(entry)
        except OSError as exc:
            _log.warning(
                "turns.read_failed",
                extra={"path": str(path), "error": str(exc)},
            )
        return out


# --------------------------------------------------------------- constructor


def new_event(
    *,
    turn_id: str,
    kind: str,
    session_key: str,
    meta: dict[str, Any] | None = None,
    ts: float | None = None,
    event_id: str | None = None,
) -> TurnEvent:
    """Convenience constructor. Stamps `ts = time()` and
    auto-generates `event_id` unless provided.

    Tests pin specific `ts` and `event_id` values when they
    want to assert on exact output. Production callers leave
    both unset.

    Not validating `kind` against :data:`TURN_EVENT_KINDS` by
    design — P6 says new kinds are additive and adding a
    writer that emits a kind not yet in the constant shouldn't
    require updating this function first."""
    import uuid

    return TurnEvent(
        turn_id=turn_id,
        event_id=event_id if event_id is not None else str(uuid.uuid4()),
        kind=kind,
        ts=ts if ts is not None else time(),
        session_key=session_key,
        meta=dict(meta) if meta else {},
    )
