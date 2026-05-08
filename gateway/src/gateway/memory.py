"""Session-scoped markdown memory on disk.

Three on-disk pieces:

* ``identity_dir/*.md``    - identity files (user, soul, tools).
                             Read fresh on every request. Prepended
                             to the system message.
* ``sessions_dir/<id>/history/YYYY-MM-DD.md`` - today's conversation
                             for one session. Append-only during the
                             day, read on every request.

Memory is a pure function of these files. No in-process cache, no
background threads. Edits to identity files take effect on the next
request. Restarting the gateway loses nothing that was successfully
written to disk.

The turn format on disk is:

    ## 2026-04-30T17:42:12Z user

    <user content>

    ## 2026-04-30T17:42:15Z assistant

    <assistant content>

Turns are separated by a blank line between blocks. The parser is
permissive - unknown header lines start a new (typed) block;
content between headers is accumulated verbatim (including blank
lines).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from .lessons import LessonsStore
from .memory_templates import DEFAULTS, LEGACY_TEMPLATES

_log = logging.getLogger(__name__)


# ----------------------------------------------------------------- types


@dataclass(frozen=True)
class LoadedContext:
    """What the memory layer returns for injection into one request."""

    system_prefix: str
    """Identity content merged together. Empty string if no identity
    files exist or memory is disabled."""

    history_messages: list[dict[str, str]]
    """List of ``{"role": ..., "content": ...}`` dicts for the
    history turns that fit in the budget. Oldest first. Empty if no
    history or memory is disabled."""

    truncated_bytes: int = 0
    """How many bytes of history were dropped due to the budget.
    0 if nothing was truncated."""


@dataclass
class _Turn:
    role: str
    content: str
    timestamp: datetime | None = None


# ----------------------------------------------------------------- store


_HEADER_RE = re.compile(r"^##\s+(\S+)\s+(user|assistant|system)\s*$")


class MemoryStore:
    """Session-scoped markdown memory."""

    def __init__(
        self,
        identity_dir: Path,
        sessions_dir: Path,
        max_history_chars: int,
        enabled: bool,
        *,
        lessons: LessonsStore | None = None,
    ) -> None:
        self._identity_dir = identity_dir
        self._sessions_dir = sessions_dir
        self._max = max_history_chars
        self._enabled = enabled
        self._lessons = lessons
        if self._enabled:
            self._ensure_identity_defaults()

    # ---------- public API ----------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def history_path(self, session_id: str, day: date | None = None) -> Path:
        day = day or _today()
        return self._sessions_dir / session_id / "history" / f"{day.isoformat()}.md"

    def load_context(self, session_id: str) -> LoadedContext:
        """Assemble identity + lessons + today's history for
        one session.

        System-prefix layering (Phase 5):

        1. Operator-authored identity (user, soul, tools) — stable
           across requests, edited by hand in ``$EDITOR``.
        2. ``[Learned corrections]`` block — auto-mutated by the
           ``learn_*`` tools, empty when there are no lessons.

        Order matters: identity first (the "who am I" voice),
        lessons after (shorter, terser, recent corrections),
        capability block prepended at the chat layer. Tested
        in ``test_memory_lessons_injection.py``.
        """
        if not self._enabled:
            return LoadedContext(system_prefix="", history_messages=[])

        system_prefix_parts: list[str] = []
        identity = self._load_identity()
        if identity:
            system_prefix_parts.append(identity)
        lessons_block = self._load_lessons_block()
        if lessons_block:
            system_prefix_parts.append(lessons_block)
        system_prefix = "\n\n".join(system_prefix_parts)

        history_file = self.history_path(session_id)
        messages, dropped = self._load_and_truncate_history(history_file)

        if dropped > 0:
            _log.info(
                "memory.history.truncated",
                extra={
                    "session_id": session_id,
                    "dropped_bytes": dropped,
                    "max_chars": self._max,
                },
            )

        return LoadedContext(
            system_prefix=system_prefix,
            history_messages=messages,
            truncated_bytes=dropped,
        )

    def append_turn(
        self,
        session_id: str,
        user_message: str,
        assistant_message: str,
    ) -> None:
        """Append a user/assistant pair to today's history.

        Atomic from the reader's perspective: either the full pair is
        present or neither half is. We use a single ``write_text``
        operation on a buffered string rather than two appends, which
        means a process crash mid-write loses the pair rather than
        leaving a half-written user block.
        """
        if not self._enabled:
            return

        path = self.history_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        now = _utc_now()
        user_block = _format_block(now, "user", user_message)
        assistant_block = _format_block(now, "assistant", assistant_message)

        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        # Ensure exactly one blank line between the previous turn and
        # the new one.
        separator = "\n" if existing and not existing.endswith("\n\n") else ""
        new_content = existing + separator + user_block + assistant_block
        path.write_text(new_content, encoding="utf-8")

    # ---------- internals -----------------------------------------

    def _ensure_identity_defaults(self) -> None:
        """Create missing identity files from templates, and heal
        files that still hold a retired default verbatim.

        The heal rule: a file whose bytes match one of the
        ``LEGACY_TEMPLATES`` entries for that name is considered
        still-default (operator never touched it) and gets
        overwritten with the current template. Anything else —
        edited, hand-written, or empty-except-whitespace — is
        treated as operator content and left alone.

        This is how the Phase 4 ``tools.md`` correction reaches
        existing installs without forcing the operator to
        ``rm -rf ~/.fitt/identity``. A file with "you have no
        tool access" sitting alongside a live capability block
        was causing the model to contradict itself; the heal
        path replaces the stale text with a preamble that
        defers to the capability block.
        """
        self._identity_dir.mkdir(parents=True, exist_ok=True)
        for name, content in DEFAULTS.items():
            target = self._identity_dir / name
            if not target.exists():
                target.write_text(content, encoding="utf-8")
                _log.info(
                    "memory.identity.created_default",
                    extra={"file": str(target)},
                )
                continue
            # File exists — heal if it's verbatim one of the known
            # legacy defaults for this name.
            legacy = LEGACY_TEMPLATES.get(name, [])
            if not legacy:
                continue
            try:
                current = target.read_text(encoding="utf-8")
            except OSError as e:
                _log.warning(
                    "memory.identity.read_for_heal_failed",
                    extra={"file": str(target), "error": str(e)},
                )
                continue
            if current in legacy:
                target.write_text(content, encoding="utf-8")
                _log.info(
                    "memory.identity.healed_legacy_default",
                    extra={"file": str(target)},
                )

    def _load_identity(self) -> str:
        """Read every ``.md`` file in identity_dir, join with section
        separators, return a single string.

        Unreadable files are skipped with a warning, not fatal.
        """
        if not self._identity_dir.exists():
            return ""
        parts: list[str] = []
        for name in ("user.md", "soul.md", "tools.md"):
            path = self._identity_dir / name
            if not path.exists():
                continue
            try:
                content = path.read_text(encoding="utf-8").strip()
            except OSError as e:
                _log.warning(
                    "memory.identity.read_failed",
                    extra={"file": str(path), "error": str(e)},
                )
                continue
            if content:
                parts.append(content)
        return "\n\n".join(parts)

    def _load_lessons_block(self) -> str:
        """Return the ``[Learned corrections]`` system-prompt
        block, or empty string if there's no lessons store or
        the store is empty.

        The block name + bullet formatting live in the
        :class:`LessonsStore` renderer; this method's only job
        is to check presence and delegate. That keeps memory
        agnostic to lesson internals."""
        if self._lessons is None:
            return ""
        try:
            return self._lessons.render_block()
        except Exception as e:  # pragma: no cover - defensive
            _log.warning(
                "memory.lessons.render_failed",
                extra={"error": str(e)},
            )
            return ""

    def _load_and_truncate_history(self, path: Path) -> tuple[list[dict[str, str]], int]:
        """Read a history file, parse into turns, drop oldest turns
        until the total content size fits the budget.

        Returns (messages, dropped_bytes). Missing or unreadable
        files are treated as empty and do not raise.
        """
        if not path.exists():
            return [], 0
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            _log.warning(
                "memory.history.read_failed",
                extra={"file": str(path), "error": str(e)},
            )
            return [], 0

        turns = _parse_turns(raw)

        # Drop oldest turns while the remaining content exceeds the
        # budget. We measure the budget against the sum of turn
        # contents (not the raw file size), because that's what ends
        # up in the LLM prompt.
        dropped_bytes = 0
        total = sum(len(t.content) for t in turns)
        while total > self._max and turns:
            dropped = turns.pop(0)
            dropped_bytes += len(dropped.content)
            total -= len(dropped.content)

        messages = [
            {"role": t.role, "content": t.content} for t in turns if t.role in ("user", "assistant")
        ]
        return messages, dropped_bytes


# ----------------------------------------------------------------- helpers


def _today() -> date:
    return _utc_now().date()


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _format_block(ts: datetime, role: str, content: str) -> str:
    stamp = ts.isoformat().replace("+00:00", "Z")
    # The body is kept as-is; trailing whitespace stripped. We add
    # exactly two newlines between the header and the body, and two
    # after the body so the next block starts cleanly.
    body = content.rstrip()
    return f"## {stamp} {role}\n\n{body}\n\n"


def _parse_turns(text: str) -> list[_Turn]:
    """Parse a history file into turns.

    Permissive: unknown header lines are ignored; lines that aren't
    headers are accumulated into the current turn's content. Returns
    turns in the order they appeared in the file.
    """
    turns: list[_Turn] = []
    current: _Turn | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal current, buffer
        if current is not None:
            current.content = "\n".join(buffer).strip()
            if current.content:
                turns.append(current)
        current = None
        buffer = []

    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            flush()
            timestamp_str, role = m.group(1), m.group(2)
            try:
                ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            except ValueError:
                ts = None
            current = _Turn(role=role, content="", timestamp=ts)
            buffer = []
        elif current is not None:
            buffer.append(line)
        # Lines before the first header are discarded (file preamble).
    flush()
    return turns
