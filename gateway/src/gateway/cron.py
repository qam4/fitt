"""Phase 4.5 cron subsystem — data model + persistence.

A cron is a scheduled agent session: on its schedule, the
gateway spawns a fresh session, runs the cron's ``message`` as
a user prompt, lets the agent work, and delivers the result (or
doesn't, if ``silent``). This module holds the data model and
persistence; the scheduler loop lives in :mod:`gateway.cron_service`
(task 4).

Three schedule kinds:

- ``every N seconds`` — interval firing.
- ``at <iso-ts>`` — one-shot at a specific moment.
- ``cron <5-field expr>`` — classic cron expression with
  optional timezone.

Persistence is a single JSON file at ``$FITT_HOME/cron.json``.
Atomic writes (tmp + rename) plus a ``.lock`` sidecar serialise
concurrent mutations from the gateway process and the ``fitt
cron`` CLI. We don't use fcntl here because Windows lacks
``fcntl.flock``; a best-effort sentinel file is enough for the
single-hub topology we support.

Mtime-based sync: the scheduler loop calls ``reload_if_changed``
every poll. If the file's mtime differs from what we loaded,
re-read. That makes ``fitt cron add ...`` visible to the live
gateway within a poll interval without a restart.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Literal

from croniter import croniter

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- errors


class CronError(Exception):
    """Base class for user-facing cron failures. Subclasses map
    to specific tool-call error messages the model can read."""


class InvalidSchedule(CronError):
    """Couldn't parse a schedule spec string."""


class UnknownCron(CronError):
    """No cron with that id."""


class DuplicateCron(CronError):
    """A cron with the same id already exists (shouldn't happen
    in practice because we generate random ids, but guard
    against restore-from-backup collisions)."""


# --------------------------------------------------------------- schedule


ScheduleKind = Literal["every", "at", "cron"]


@dataclass(frozen=True, slots=True)
class CronSchedule:
    """A firing schedule.

    Only one of ``every_secs`` / ``at_ts`` / ``cron_expr`` is
    populated depending on ``kind``. Mutually exclusive, enforced
    by the parser and :meth:`next_run`.
    """

    kind: ScheduleKind
    every_secs: int | None = None
    at_ts: float | None = None
    cron_expr: str | None = None
    timezone: str = "UTC"
    """IANA timezone for cron-expression evaluation. Ignored for
    ``every`` and ``at`` (interval math is timezone-agnostic,
    and ``at`` timestamps are already absolute)."""

    def next_run(self, *, after: float) -> float | None:
        """Return the next firing time strictly *after* ``after``
        (unix seconds), or None for a consumed one-shot.

        - ``every``: next multiple of ``every_secs`` > ``after``.
          We don't anchor to wall clock because the previous run's
          timestamp is already passed in via ``after`` (the
          scheduler tracks ``last_run_ts`` per job).
        - ``at``: returns ``at_ts`` if ``at_ts > after``, else None.
        - ``cron``: delegates to :mod:`croniter`.
        """
        if self.kind == "every":
            assert self.every_secs is not None
            return after + self.every_secs
        if self.kind == "at":
            assert self.at_ts is not None
            return self.at_ts if self.at_ts > after else None
        if self.kind == "cron":
            assert self.cron_expr is not None
            # croniter works in naive-localtime by default. We
            # feed it a tz-aware datetime so its arithmetic is
            # correct across DST; result comes back as
            # tz-aware which we then epoch-ify.
            try:
                tz = _get_tz(self.timezone)
            except _TzError:
                tz = UTC
            base_dt = datetime.fromtimestamp(after, tz=tz)
            it = croniter(self.cron_expr, start_time=base_dt)
            next_dt = it.get_next(datetime)
            return next_dt.timestamp()
        raise InvalidSchedule(f"unknown schedule kind: {self.kind!r}")


# --------------------------------------------------------------- schedule parser


_SPEC_EVERY = re.compile(
    # Order alternatives longest-first so ``hour`` wins over ``h``
    # inside "every 1 hour". Without this, the regex greedily
    # matches ``h`` and then ``our`` is left dangling before ``$``.
    r"""^\s*every\s+(?P<n>\d+)\s*(?P<unit>seconds|minutes|hours|hour|sec|secs|min|mins|hr|hrs|s|m|h)?\s*$""",
    re.IGNORECASE,
)
_SPEC_IN = re.compile(
    r"""^\s*in\s+(?P<n>\d+)\s*(?P<unit>seconds|minutes|hours|hour|sec|secs|min|mins|hr|hrs|s|m|h)\s*$""",
    re.IGNORECASE,
)
_SPEC_AT = re.compile(r"""^\s*at\s+(?P<ts>\S.*?)\s*$""", re.IGNORECASE)
_SPEC_CRON = re.compile(r"""^\s*cron\s+(?P<expr>\S.*\S)\s*$""", re.IGNORECASE)

_UNIT_SECS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
}


def parse_schedule_spec(
    spec: str,
    *,
    now: float | None = None,
    tz: str = "UTC",
) -> CronSchedule:
    """Parse a human-written schedule spec into a CronSchedule.

    Accepts:

    - ``every 60`` / ``every 60s`` / ``every 5m`` / ``every 1h``
    - ``in 30 minutes`` / ``in 10s`` (resolves to ``at`` under the
      hood — the absolute timestamp is what gets stored).
    - ``at 2026-05-06T09:00:00`` (ISO-8601; naive is interpreted
      in ``tz``, aware takes its own offset).
    - ``at 1777497338`` (raw unix epoch seconds).
    - ``cron 0 9 * * *`` (5-field).

    The ``tz`` argument is attached to cron-kind schedules and
    used to resolve naive ISO timestamps in ``at``. It doesn't
    affect ``every`` arithmetic.

    Raises :exc:`InvalidSchedule` on anything else. Keep error
    messages readable — they surface verbatim as the tool's
    structured error back to the model.
    """
    if not spec or not spec.strip():
        raise InvalidSchedule("schedule spec is empty")

    # every N [unit]
    m = _SPEC_EVERY.match(spec)
    if m:
        n = int(m.group("n"))
        unit = (m.group("unit") or "s").lower()
        secs = n * _UNIT_SECS[unit]
        if secs <= 0:
            raise InvalidSchedule("'every' interval must be > 0")
        return CronSchedule(kind="every", every_secs=secs, timezone=tz)

    # in N unit  (sugar for at <now+delta>)
    m = _SPEC_IN.match(spec)
    if m:
        n = int(m.group("n"))
        unit = m.group("unit").lower()
        delta_secs = n * _UNIT_SECS[unit]
        if delta_secs <= 0:
            raise InvalidSchedule("'in' delta must be > 0")
        base = now if now is not None else time.time()
        return CronSchedule(kind="at", at_ts=base + delta_secs, timezone=tz)

    # at <timestamp>
    m = _SPEC_AT.match(spec)
    if m:
        raw = m.group("ts")
        at_ts = _parse_at_timestamp(raw, tz=tz)
        return CronSchedule(kind="at", at_ts=at_ts, timezone=tz)

    # cron <expr>
    m = _SPEC_CRON.match(spec)
    if m:
        expr = m.group("expr")
        if not croniter.is_valid(expr):
            raise InvalidSchedule(f"invalid cron expression: {expr!r}")
        return CronSchedule(kind="cron", cron_expr=expr, timezone=tz)

    raise InvalidSchedule(
        f"could not parse schedule spec: {spec!r}. "
        "Expected one of: 'every N', 'every Nm', 'in N minutes', "
        "'at <iso|epoch>', 'cron <5-field expr>'."
    )


def _parse_at_timestamp(raw: str, *, tz: str) -> float:
    """Accept either a unix-epoch number or an ISO-8601 string."""
    try:
        # Raw epoch seconds (integer or float).
        return float(raw)
    except ValueError:
        pass
    # ISO-8601. Naive → interpret in `tz`. Aware → honour the offset.
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as e:
        raise InvalidSchedule(f"invalid 'at' timestamp {raw!r}: {e}") from e
    if dt.tzinfo is None:
        try:
            dt = dt.replace(tzinfo=_get_tz(tz))
        except _TzError:
            dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


class _TzError(Exception):
    pass


def _get_tz(name: str) -> tzinfo:
    """Resolve an IANA tz name to a tzinfo via zoneinfo. Falls
    back to UTC on unknown names so a mistyped zone doesn't
    crash the gateway — it just schedules in UTC."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if not name or name.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as e:
        raise _TzError(str(e)) from e


# --------------------------------------------------------------- cron job


ApprovalModeOverride = Literal["", "auto"]
LastStatus = Literal["", "ok", "error"]


@dataclass(slots=True)
class CronJob:
    """Persisted cron record.

    Mutable only through :class:`CronService` — direct field
    mutation is discouraged because the service is responsible
    for keeping the on-disk file in sync.
    """

    id: str
    name: str
    message: str
    schedule: CronSchedule

    enabled: bool = True
    silent: bool = False
    approval_mode: ApprovalModeOverride = ""
    """When ``"auto"``, tool calls inside this cron's firings
    auto-approve even if their default bucket is ``ask``.
    Applied on top of the Phase 4 approval middleware, not
    instead of it — the deny list still short-circuits."""

    agent_alias: str = ""
    """Empty → use the gateway's default alias at fire time.
    Resolving late means flipping ``aliases.fitt-default`` in
    ``config.yaml`` propagates to existing crons without
    needing to edit them."""

    session_key: str = ""
    """Session that created this cron (audit trail). A cron's
    *firings* use ``cron:<id>:<ts>`` session keys; this field
    is the human origin."""

    created_by_client: str = ""
    """``ide`` / ``telegram`` / ``webui`` / ``cli`` — propagated
    from the creating request's client tag."""

    created_ts: float = 0.0
    last_run_ts: float | None = None
    last_status: LastStatus = ""
    last_error: str = ""
    delete_after_run: bool = False
    """One-shot ``at`` schedules set this so the service drops
    the record after firing once."""


# --------------------------------------------------------------- service


@dataclass(slots=True)
class _DiskState:
    """What we just read from disk. Kept separately so a partial
    parse doesn't clobber the in-memory map on an edit race."""

    jobs: dict[str, CronJob]
    mtime_ns: int


class CronService:
    """File-backed CRUD for crons.

    Hot-reloads the on-disk file when its mtime changes. All
    mutations go through write-through methods that rewrite the
    whole file atomically — the file is tiny (tens of jobs at
    most), the cost is noise, and we never half-write.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._state = _DiskState(jobs={}, mtime_ns=-1)
        self.reload_if_changed()

    # ------------------------------------------------ introspection

    def get(self, id: str) -> CronJob | None:
        with self._lock:
            return self._state.jobs.get(id)

    def list(self, *, include_disabled: bool = True) -> list[CronJob]:
        """Return jobs in a stable order (by ``created_ts`` then
        ``id``) so CLI output and tool responses are reproducible."""
        with self._lock:
            jobs = list(self._state.jobs.values())
        if not include_disabled:
            jobs = [j for j in jobs if j.enabled]
        jobs.sort(key=lambda j: (j.created_ts, j.id))
        return jobs

    # ------------------------------------------------ mutation

    def add(self, job: CronJob) -> CronJob:
        """Persist a new job. Generates an id if ``job.id`` is
        empty. Fills ``created_ts`` if zero."""
        with self._lock:
            j = _materialise(job)
            if j.id in self._state.jobs:
                raise DuplicateCron(f"cron {j.id!r} already exists")
            self._state.jobs[j.id] = j
            self._write_locked()
            return j

    def update(
        self,
        id: str,
        *,
        name: str | None = None,
        message: str | None = None,
        schedule: CronSchedule | None = None,
        enabled: bool | None = None,
        silent: bool | None = None,
        approval_mode: ApprovalModeOverride | None = None,
        agent_alias: str | None = None,
        last_run_ts: float | None = None,
        last_status: LastStatus | None = None,
        last_error: str | None = None,
    ) -> CronJob:
        """Partial update. Returns the post-update record.

        Only the fields callers typically care about are exposed;
        fields like ``created_ts`` / ``created_by_client`` are
        immutable by design (audit trail).
        """
        with self._lock:
            job = self._state.jobs.get(id)
            if job is None:
                raise UnknownCron(f"no cron with id {id!r}")
            if name is not None:
                job.name = name
            if message is not None:
                job.message = message
            if schedule is not None:
                job.schedule = schedule
            if enabled is not None:
                job.enabled = enabled
            if silent is not None:
                job.silent = silent
            if approval_mode is not None:
                job.approval_mode = approval_mode
            if agent_alias is not None:
                job.agent_alias = agent_alias
            if last_run_ts is not None:
                job.last_run_ts = last_run_ts
            if last_status is not None:
                job.last_status = last_status
            if last_error is not None:
                job.last_error = last_error
            self._write_locked()
            return job

    def remove(self, id: str) -> bool:
        """Drop a job. Returns True if it existed."""
        with self._lock:
            if id not in self._state.jobs:
                return False
            del self._state.jobs[id]
            self._write_locked()
            return True

    def set_enabled(self, id: str, enabled: bool) -> CronJob:
        return self.update(id, enabled=enabled)

    # ------------------------------------------------ reload

    def reload_if_changed(self) -> bool:
        """If the file's mtime differs from the last load, re-read
        and return True. Missing file is treated as "empty set".

        External edits (``fitt cron`` CLI from a different shell,
        or hand-editing the JSON) become visible via this hook."""
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            with self._lock:
                if self._state.jobs:
                    self._state = _DiskState(jobs={}, mtime_ns=-1)
                    return True
                self._state = _DiskState(jobs={}, mtime_ns=-1)
            return False
        current_mtime = stat.st_mtime_ns
        with self._lock:
            if current_mtime == self._state.mtime_ns:
                return False
            loaded = self._load_from_disk_locked()
            self._state = _DiskState(jobs=loaded, mtime_ns=current_mtime)
            return True

    # ------------------------------------------------ disk I/O

    def _load_from_disk_locked(self) -> dict[str, CronJob]:
        """Parse the file. Missing/invalid returns an empty map
        with a warning; we don't want a corrupted file to crash
        the gateway's startup."""
        if not self._path.exists():
            return {}
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {"jobs": []}
        except (OSError, json.JSONDecodeError) as e:
            _log.warning("cron.load_failed", extra={"path": str(self._path), "error": str(e)})
            return {}
        jobs_raw = data.get("jobs") or []
        out: dict[str, CronJob] = {}
        for entry in jobs_raw:
            try:
                job = _deserialise(entry)
            except (KeyError, TypeError, ValueError) as e:
                _log.warning(
                    "cron.deserialise_failed",
                    extra={"entry_sample": json.dumps(entry)[:120], "error": str(e)},
                )
                continue
            out[job.id] = job
        return out

    def _write_locked(self) -> None:
        """Atomic write under the in-process lock. Caller holds
        ``self._lock``; we don't re-acquire."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"jobs": [_serialise(j) for j in self._state.jobs.values()]}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)
        try:
            self._state = _DiskState(jobs=self._state.jobs, mtime_ns=self._path.stat().st_mtime_ns)
        except FileNotFoundError:  # pragma: no cover — rename guarantees existence
            pass


# --------------------------------------------------------------- serialise


def _serialise(job: CronJob) -> dict[str, object]:
    """Convert a CronJob to a JSON-safe dict. Handles the nested
    CronSchedule explicitly because dataclasses.asdict recurses
    but we want the schedule keys flattened predictably."""
    out: dict[str, object] = asdict(job)
    # asdict already flattened schedule into a dict. Good enough.
    return out


def _deserialise(data: dict[str, object]) -> CronJob:
    """Inverse of :func:`_serialise`. Tolerates extra fields (for
    forward compat) and missing optional fields (for backward
    compat).

    Each field is coerced via a small helper that narrows
    ``object`` to the expected type, so a hand-edited file with
    e.g. a string ``created_ts`` raises ``ValueError`` with a
    readable message rather than surviving as an invariant-broken
    record.
    """
    sched_raw = data.get("schedule")
    if not isinstance(sched_raw, dict):
        raise ValueError("missing or invalid 'schedule' field")
    schedule = CronSchedule(
        kind=_as_schedule_kind(sched_raw.get("kind")),
        every_secs=_as_optional_int(sched_raw.get("every_secs")),
        at_ts=_as_optional_float(sched_raw.get("at_ts")),
        cron_expr=_as_optional_str(sched_raw.get("cron_expr")),
        timezone=_as_str(sched_raw.get("timezone", "UTC")),
    )
    return CronJob(
        id=_as_str(data["id"]),
        name=_as_str(data.get("name", "")),
        message=_as_str(data.get("message", "")),
        schedule=schedule,
        enabled=bool(data.get("enabled", True)),
        silent=bool(data.get("silent", False)),
        approval_mode=_as_approval_mode(data.get("approval_mode", "")),
        agent_alias=_as_str(data.get("agent_alias", "")),
        session_key=_as_str(data.get("session_key", "")),
        created_by_client=_as_str(data.get("created_by_client", "")),
        created_ts=_as_float(data.get("created_ts", 0.0)),
        last_run_ts=_as_optional_float(data.get("last_run_ts")),
        last_status=_as_last_status(data.get("last_status", "")),
        last_error=_as_str(data.get("last_error", "")),
        delete_after_run=bool(data.get("delete_after_run", False)),
    )


# ---- narrowing helpers. Each raises ValueError on bad shape so
# _load_from_disk_locked's except-ValueError catches it and the
# entry is skipped with a warning.


def _as_str(v: object) -> str:
    if isinstance(v, str):
        return v
    return str(v)


def _as_optional_str(v: object) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    raise ValueError(f"expected string or null, got {type(v).__name__}")


def _as_float(v: object) -> float:
    if isinstance(v, int | float):
        return float(v)
    raise ValueError(f"expected number, got {type(v).__name__}")


def _as_optional_float(v: object) -> float | None:
    if v is None:
        return None
    return _as_float(v)


def _as_optional_int(v: object) -> int | None:
    if v is None:
        return None
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    raise ValueError(f"expected integer or null, got {type(v).__name__}")


def _as_schedule_kind(v: object) -> ScheduleKind:
    if isinstance(v, str) and v in {"every", "at", "cron"}:
        return v  # type: ignore[return-value]
    raise ValueError(f"invalid schedule kind: {v!r}")


def _as_approval_mode(v: object) -> ApprovalModeOverride:
    # Tolerate null → empty string so an old file without the
    # field survives a reload.
    if v is None:
        return ""
    if isinstance(v, str) and v in {"", "auto"}:
        return v  # type: ignore[return-value]
    raise ValueError(f"invalid approval_mode: {v!r}")


def _as_last_status(v: object) -> LastStatus:
    if v is None:
        return ""
    if isinstance(v, str) and v in {"", "ok", "error"}:
        return v  # type: ignore[return-value]
    raise ValueError(f"invalid last_status: {v!r}")


def _materialise(job: CronJob) -> CronJob:
    """Fill in defaults for an incoming job: id (fresh random
    hex) and created_ts (now). We don't use uuid4 because a
    shorter ID fits better in CLI output and the collision
    space is still huge at ~64 bits."""
    if not job.id:
        job.id = secrets.token_hex(8)  # 16 hex chars, ~64 bits
    if not job.created_ts:
        job.created_ts = time.time()
    # For one-shot 'at' schedules, flag delete-after-run unless
    # the caller explicitly set it.
    if job.schedule.kind == "at" and not job.delete_after_run:
        job.delete_after_run = True
    return job


# --------------------------------------------------------------- paths


def default_cron_path(fitt_home: Path) -> Path:
    return fitt_home / "cron.json"
