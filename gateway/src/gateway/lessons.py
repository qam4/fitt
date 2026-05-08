"""Phase 5 — lessons store.

Adds a fourth identity file, ``$FITT_HOME/identity/lessons.md``,
that's auto-mutated by the ``learn_*`` inline tools and the
``fitt learn`` CLI. The file is also hand-editable — operators
can open it in a text editor at any time; the next request
picks up the change via mtime-based reload.

Design shape mirrors :class:`gateway.cron.CronService`:

- Single-file on-disk state. Small, human-readable, grep-able.
- Write-through mutations (read → parse → mutate → write) under
  a thread lock. Rewriting on every mutation is fine because
  the file is tiny (≤50 entries by default).
- mtime-based reload for external edits.
- Plain :func:`threading.Lock` rather than fcntl — Windows
  compat, single-hub topology, and the file is rewritten
  atomically on every touch anyway.

Why not JSON? Two reasons. First, the file lives alongside
``user.md`` / ``soul.md`` / ``tools.md`` and the operator-facing
mental model is "a markdown file I can edit." Second, the
system-prompt rendering dumps the bullets directly — no
serialisation layer needed.

Rendering contract: :meth:`LessonsStore.render_block` returns
the exact string the memory store injects as the
``[Learned corrections]`` block. When there are no lessons the
block is empty (not even the header) — the memory store decides
what wrapper to apply.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- template


_TEMPLATE = """\
# Learned corrections

This file is auto-mutated by the `learn_add` / `learn_remove`
tools and by the `fitt learn` CLI. You can hand-edit it at any
time — the next request picks up your changes. Manual edits
may be overwritten later if the agent records a conflicting
correction; keep important preferences in `user.md` if you
want them safe from agent writes.

Lessons are short reminders carried into every system prompt
as the `[Learned corrections]` block. Keep each entry a
bullet; one or two sentences max. Long prose stops being
useful at scale.

Optional `[category]` prefix helps organise entries.

## Active lessons

"""


_ACTIVE_HEADER = "## Active lessons"
_BULLET_RE = re.compile(r"^\s*-\s+(.*\S)\s*$")
_CATEGORY_RE = re.compile(r"^\[(?P<cat>[^\]]+)\]\s+(?P<body>.+)$")


# --------------------------------------------------------------- dataclasses


@dataclass(frozen=True, slots=True)
class Lesson:
    """One learned correction.

    ``added_ts`` is best-effort: the file format doesn't record
    per-entry timestamps (human-editability trumps perfect
    provenance), so the store uses the file's mtime as a group
    proxy and sets ``added_ts = 0.0`` for entries loaded from
    disk. Entries freshly added via :meth:`add` get ``time()``
    so the in-process ordering stays correct for the current
    session. Tests can tolerate this by not asserting on
    ``added_ts`` after a round-trip.
    """

    text: str
    category: str | None = None
    added_ts: float = 0.0

    def render(self) -> str:
        """One bullet line, markdown."""
        if self.category:
            return f"- [{self.category}] {self.text}"
        return f"- {self.text}"

    @classmethod
    def from_bullet(cls, bullet: str) -> Lesson:
        """Parse a bullet body into a :class:`Lesson`.

        Accepts optional ``[category]`` prefix. Trailing /
        leading whitespace stripped. Unknown shapes degrade
        to a category-less lesson with the raw body."""
        stripped = bullet.strip()
        m = _CATEGORY_RE.match(stripped)
        if m:
            return cls(text=m.group("body").strip(), category=m.group("cat").strip())
        return cls(text=stripped, category=None)


# --------------------------------------------------------------- store


class LessonsStore:
    """File-backed CRUD for lessons.

    One instance per gateway process. All reads honour the
    current file mtime — an external edit is picked up on the
    next :meth:`read` without a gateway restart.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_entries: int = 50,
    ) -> None:
        self._path = path
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._cached: list[Lesson] = []
        self._mtime_ns: int = -1
        # Ensure the file exists and carries the template on
        # first boot. Operators who delete it manually get it
        # re-created on the next mutation / read.
        self._ensure_file()

    # ------------------------------------------------ public

    def read(self) -> list[Lesson]:
        """Return the current lesson list.

        mtime-aware: re-reads from disk if the file has changed
        since the last call. Cheap on the common path — one
        stat + cache return."""
        with self._lock:
            return list(self._read_locked())

    def add(self, text: str, *, category: str | None = None) -> Lesson:
        """Append a new lesson.

        If the ceiling is reached, the oldest entry drops. The
        "oldest" is the first bullet in the file (the order
        represents append history — first-added first).

        Returns the lesson that was added."""
        if not text or not text.strip():
            raise ValueError("lesson text must be non-empty")
        lesson = Lesson(
            text=text.strip(),
            category=(category.strip() if category and category.strip() else None),
            added_ts=time.time(),
        )
        with self._lock:
            lessons = list(self._read_locked())
            lessons.append(lesson)
            # Drop oldest until we're under the ceiling.
            while len(lessons) > self._max_entries:
                dropped = lessons.pop(0)
                _log.info(
                    "lessons.capacity_drop",
                    extra={
                        "dropped_text": dropped.text[:120],
                        "max_entries": self._max_entries,
                    },
                )
            self._write_locked(lessons)
            return lesson

    def remove(self, substring: str) -> int:
        """Remove every lesson whose text contains
        ``substring`` (case-insensitive substring match, to
        match how a user would phrase the removal). Returns
        the count removed."""
        needle = (substring or "").strip().lower()
        if not needle:
            return 0
        with self._lock:
            lessons = list(self._read_locked())
            kept = [lsn for lsn in lessons if needle not in lsn.text.lower()]
            removed = len(lessons) - len(kept)
            if removed > 0:
                self._write_locked(kept)
            return removed

    def render_block(self) -> str:
        """Return the string the memory store injects as the
        ``[Learned corrections]`` block.

        Empty when there are no lessons — the memory store
        suppresses the whole block in that case."""
        lessons = self.read()
        if not lessons:
            return ""
        lines = ["[Learned corrections]", ""]
        lines.extend(lsn.render() for lsn in lessons)
        return "\n".join(lines)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def max_entries(self) -> int:
        return self._max_entries

    # ------------------------------------------------ internals

    def _ensure_file(self) -> None:
        """Write the template on first boot if the file is
        missing. Doesn't touch an existing file; operator
        content wins."""
        if self._path.exists():
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(_TEMPLATE, encoding="utf-8")
        _log.info("lessons.template_written", extra={"path": str(self._path)})

    def _read_locked(self) -> list[Lesson]:
        """Re-read from disk if the mtime has changed. Caller
        holds ``self._lock``."""
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            # Someone (operator?) deleted the file between our
            # constructor and now. Rewrite the template so
            # the next mutation has something to edit.
            self._ensure_file()
            self._cached = []
            try:
                self._mtime_ns = self._path.stat().st_mtime_ns
            except FileNotFoundError:  # pragma: no cover - defensive
                self._mtime_ns = -1
            return self._cached
        if stat.st_mtime_ns == self._mtime_ns and self._cached is not None:
            return self._cached
        parsed = self._parse_file()
        self._cached = parsed
        self._mtime_ns = stat.st_mtime_ns
        return parsed

    def _parse_file(self) -> list[Lesson]:
        """Parse the lessons file into a list of Lesson.

        Format: anything before ``## Active lessons`` is the
        operator-facing preamble and ignored. After that header,
        bullet lines (``- text`` or ``- [cat] text``) are parsed
        as lessons. Other lines are treated as whitespace /
        commentary and dropped. A malformed file that doesn't
        contain the header logs a warning and returns empty —
        the next mutation rewrites with the template.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as e:
            _log.warning(
                "lessons.read_failed",
                extra={"path": str(self._path), "error": str(e)},
            )
            return []
        if _ACTIVE_HEADER not in raw:
            _log.warning(
                "lessons.missing_header",
                extra={
                    "path": str(self._path),
                    "expected_header": _ACTIVE_HEADER,
                    "hint": (
                        "the file was edited into a shape the "
                        "parser doesn't recognise; delete it to "
                        "regenerate the template, then re-add "
                        "your lessons"
                    ),
                },
            )
            return []
        _, _, body = raw.partition(_ACTIVE_HEADER)
        lessons: list[Lesson] = []
        for line in body.splitlines():
            m = _BULLET_RE.match(line)
            if m:
                lessons.append(Lesson.from_bullet(m.group(1)))
        return lessons

    def _write_locked(self, lessons: list[Lesson]) -> None:
        """Serialise ``lessons`` under the current template
        preamble. Caller holds ``self._lock``."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            raw = _TEMPLATE
        if _ACTIVE_HEADER in raw:
            preamble, _, _ = raw.partition(_ACTIVE_HEADER)
            preamble = preamble + _ACTIVE_HEADER + "\n\n"
        else:
            preamble = _TEMPLATE
        rendered = preamble + "\n".join(lsn.render() for lsn in lessons)
        if lessons:
            rendered += "\n"
        self._path.write_text(rendered, encoding="utf-8")
        try:
            self._mtime_ns = self._path.stat().st_mtime_ns
        except FileNotFoundError:  # pragma: no cover - defensive
            self._mtime_ns = -1
        self._cached = list(lessons)


# --------------------------------------------------------------- defaults


def default_lessons_path(identity_dir: Path) -> Path:
    """Convention: lessons.md lives alongside the identity
    files at ``$FITT_HOME/identity/lessons.md``. Same directory
    because the mental model is "everything that feeds the
    system prompt"; different file because the lifecycle is
    agent-mutated vs. operator-authored."""
    return identity_dir / "lessons.md"
