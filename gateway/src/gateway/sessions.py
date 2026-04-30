"""Sessions: named conversation scopes, persisted on disk.

A session is a logical conversation scope. All chat requests with
``X-FITT-Session: <id>`` share one history file per day
(``sessions/<id>/history/YYYY-MM-DD.md``), so a user can keep a
retro-ai conversation separate from a home-automation conversation
without contamination.

Identity is NOT per-session. There's one user, one persona; both
sessions read the same ``identity/*.md``.

The index of known sessions lives at
``sessions/sessions.json``. The ``main`` session is always present
and cannot be renamed, archived, or deleted.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Request

from .errors import GatewayError, UnknownSession

_log = logging.getLogger(__name__)

DEFAULT_SESSION_ID = "main"
SESSION_HEADER = "X-FITT-Session"

SESSION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
"""Lowercase, digits, hyphens. No leading hyphen, no length limit
enforced by the regex (a reasonable 64-char cap is applied in
``create``)."""

_MAX_ID_LEN = 64


# ----------------------------------------------------------------- data


@dataclass(frozen=True)
class Session:
    id: str
    name: str
    created_at: datetime
    archived: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at.isoformat().replace("+00:00", "Z"),
            "archived": self.archived,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Session:
        ts = raw.get("created_at")
        if isinstance(ts, str):
            ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            ts_dt = datetime.now(UTC)
        return cls(
            id=str(raw["id"]),
            name=str(raw.get("name", raw["id"])),
            created_at=ts_dt,
            archived=bool(raw.get("archived", False)),
        )


# ----------------------------------------------------------------- errors


class SessionError(GatewayError):
    """Base for session-management errors raised by the CLI path."""


class InvalidSessionId(SessionError):
    pass


class DuplicateSessionId(SessionError):
    pass


class ProtectedSession(SessionError):
    """Raised on attempts to archive / rename / delete 'main'."""


# ----------------------------------------------------------------- registry


@dataclass
class _LoadedIndex:
    sessions: dict[str, Session] = field(default_factory=dict)


class SessionRegistry:
    """On-disk session index.

    The index is re-read on every public method call. That keeps the
    CLI ('fitt session new foo') and the gateway in sync without
    restart - the gateway sees newly-created sessions on the very
    next request.
    """

    def __init__(self, sessions_dir: Path) -> None:
        self._sessions_dir = sessions_dir
        self._index_path = sessions_dir / "sessions.json"

    # ---------- lifecycle -----------------------------------------

    def ensure_main(self) -> None:
        """Create the index (with `main`) if missing. Also re-add
        `main` if somehow absent (defensive)."""
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        idx = self._load()
        if DEFAULT_SESSION_ID not in idx.sessions:
            idx.sessions[DEFAULT_SESSION_ID] = Session(
                id=DEFAULT_SESSION_ID,
                name="Main",
                created_at=_utc_now(),
                archived=False,
            )
            self._save(idx)
            _log.info(
                "sessions.main_created",
                extra={"path": str(self._index_path)},
            )

    # ---------- read ----------------------------------------------

    def all(self, *, include_archived: bool = False) -> list[Session]:
        idx = self._load()
        out = list(idx.sessions.values())
        if not include_archived:
            out = [s for s in out if not s.archived]
        out.sort(key=lambda s: (s.id != DEFAULT_SESSION_ID, s.id))
        return out

    def get(self, session_id: str) -> Session | None:
        return self._load().sessions.get(session_id)

    def valid_ids(self) -> set[str]:
        """Active (non-archived) session ids."""
        return {s.id for s in self._load().sessions.values() if not s.archived}

    # ---------- write ---------------------------------------------

    def create(self, session_id: str, name: str | None = None) -> Session:
        self._validate_id(session_id)
        idx = self._load()
        if session_id in idx.sessions:
            raise DuplicateSessionId(f"Session {session_id!r} already exists.")
        session = Session(
            id=session_id,
            name=name or session_id,
            created_at=_utc_now(),
            archived=False,
        )
        idx.sessions[session_id] = session
        self._save(idx)
        (self._sessions_dir / session_id / "history").mkdir(parents=True, exist_ok=True)
        return session

    def rename(self, session_id: str, name: str) -> Session:
        self._require_mutable(session_id)
        idx = self._load()
        existing = idx.sessions.get(session_id)
        if existing is None:
            raise SessionError(f"Session {session_id!r} not found.")
        updated = Session(
            id=existing.id,
            name=name,
            created_at=existing.created_at,
            archived=existing.archived,
        )
        idx.sessions[session_id] = updated
        self._save(idx)
        return updated

    def archive(self, session_id: str) -> Session:
        self._require_mutable(session_id)
        return self._set_archived(session_id, True)

    def unarchive(self, session_id: str) -> Session:
        return self._set_archived(session_id, False)

    def _set_archived(self, session_id: str, archived: bool) -> Session:
        idx = self._load()
        existing = idx.sessions.get(session_id)
        if existing is None:
            raise SessionError(f"Session {session_id!r} not found.")
        updated = Session(
            id=existing.id,
            name=existing.name,
            created_at=existing.created_at,
            archived=archived,
        )
        idx.sessions[session_id] = updated
        self._save(idx)
        return updated

    # ---------- internals -----------------------------------------

    def _validate_id(self, session_id: str) -> None:
        if not SESSION_ID_PATTERN.match(session_id):
            raise InvalidSessionId(
                f"Session id {session_id!r} is invalid. Must match {SESSION_ID_PATTERN.pattern}."
            )
        if len(session_id) > _MAX_ID_LEN:
            raise InvalidSessionId(f"Session id {session_id!r} exceeds {_MAX_ID_LEN} characters.")

    def _require_mutable(self, session_id: str) -> None:
        if session_id == DEFAULT_SESSION_ID:
            raise ProtectedSession(f"Session {session_id!r} is protected and cannot be modified.")

    def _load(self) -> _LoadedIndex:
        if not self._index_path.exists():
            return _LoadedIndex()
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            _log.warning(
                "sessions.index.read_failed",
                extra={"path": str(self._index_path), "error": str(e)},
            )
            # Fall back to just `main` in memory so the gateway
            # doesn't refuse to serve.
            return _LoadedIndex(
                sessions={
                    DEFAULT_SESSION_ID: Session(
                        id=DEFAULT_SESSION_ID,
                        name="Main",
                        created_at=_utc_now(),
                        archived=False,
                    )
                }
            )
        return _parse_index(raw)

    def _save(self, idx: _LoadedIndex) -> None:
        """Write the index file atomically.

        Uses temp-file + os.replace so a crash mid-write cannot leave
        a half-written sessions.json.
        """
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "sessions": [s.to_dict() for s in idx.sessions.values()],
        }
        fd, tmp = tempfile.mkstemp(
            prefix="sessions-",
            suffix=".json.tmp",
            dir=str(self._index_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self._index_path)
        except Exception:
            # Clean up on failure. os.replace is atomic so this branch
            # only fires on write errors before the rename.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _parse_index(raw: Any) -> _LoadedIndex:
    idx = _LoadedIndex()
    if not isinstance(raw, dict):
        return idx
    entries = raw.get("sessions", [])
    if not isinstance(entries, list):
        return idx
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            s = Session.from_dict(entry)
        except (KeyError, ValueError, TypeError) as e:
            _log.warning("sessions.index.entry_skip", extra={"error": str(e)})
            continue
        idx.sessions[s.id] = s
    return idx


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


# ----------------------------------------------------------------- resolver


def resolve_session_id(request: Request, registry: SessionRegistry) -> str:
    """Return the session id for this request.

    Precedence:
      1. ``X-FITT-Session`` header if present and non-empty.
      2. Default to ``main``.

    Archived and unknown ids both raise ``UnknownSession``.
    """
    header_value = request.headers.get(SESSION_HEADER)
    if header_value is None or header_value.strip() == "":
        return DEFAULT_SESSION_ID
    session_id = header_value.strip()
    valid = registry.valid_ids()
    if session_id not in valid:
        raise UnknownSession(session_id, sorted(valid))
    return session_id
