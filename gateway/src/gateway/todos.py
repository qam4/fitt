"""Phase E — todo store (untimed, curatable task list).

The gap between cron (needs a time) and lessons (standing preferences):
a plain list of things to do ("call the doctor") with no fixed time and
done/remove semantics. Markdown-backed at ``$FITT_HOME/todos.md``,
mutated by the ``todo_*`` tools and hand-editable — mirrors
:class:`gateway.lessons.LessonsStore` (single-file, write-through under
a lock, mtime reload).

Deliberately NOT injected into the system prompt (unlike lessons):
todos are accessed on demand via ``todo_list`` so they don't add fixed
per-turn prompt overhead (see the "Prompt-size budget" observed-issue).
Items are markdown checkboxes: ``- [ ] open`` / ``- [x] done``.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

_TEMPLATE = """\
# Todos

Your task list, mutated by the `todo_add` / `todo_done` / `todo_remove`
tools and by hand. Untimed tasks (for time-based reminders, use a cron).
Each item is a markdown checkbox; the next request picks up hand-edits.

## Open

"""

_OPEN_HEADER = "## Open"
_BULLET_RE = re.compile(r"^\s*-\s*\[(?P<mark>[ xX])\]\s+(?P<body>.*\S)\s*$")


@dataclass(frozen=True, slots=True)
class Todo:
    text: str
    done: bool = False

    def render(self) -> str:
        return f"- [{'x' if self.done else ' '}] {self.text}"

    @classmethod
    def from_bullet(cls, mark: str, body: str) -> Todo:
        return cls(text=body.strip(), done=mark.lower() == "x")


class TodoStore:
    """File-backed CRUD for todos. One instance per gateway process;
    mtime-aware reads pick up external edits without a restart."""

    def __init__(self, path: Path, *, max_entries: int = 200) -> None:
        self._path = path
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._cached: list[Todo] = []
        self._mtime_ns: int = -1
        self._ensure_file()

    # ------------------------------------------------ public

    def read(self) -> list[Todo]:
        with self._lock:
            return list(self._read_locked())

    def open_todos(self) -> list[Todo]:
        return [t for t in self.read() if not t.done]

    def add(self, text: str) -> Todo:
        if not text or not text.strip():
            raise ValueError("todo text must be non-empty")
        todo = Todo(text=text.strip(), done=False)
        with self._lock:
            todos = list(self._read_locked())
            todos.append(todo)
            while len(todos) > self._max_entries:
                todos.pop(0)
            self._write_locked(todos)
            return todo

    def mark_done(self, substring: str) -> int:
        """Flip every OPEN todo whose text contains ``substring`` to
        done (case-insensitive). Returns the count flipped."""
        needle = (substring or "").strip().lower()
        if not needle:
            return 0
        with self._lock:
            todos = list(self._read_locked())
            flipped = 0
            out: list[Todo] = []
            for t in todos:
                if not t.done and needle in t.text.lower():
                    out.append(Todo(text=t.text, done=True))
                    flipped += 1
                else:
                    out.append(t)
            if flipped:
                self._write_locked(out)
            return flipped

    def remove(self, substring: str) -> int:
        needle = (substring or "").strip().lower()
        if not needle:
            return 0
        with self._lock:
            todos = list(self._read_locked())
            kept = [t for t in todos if needle not in t.text.lower()]
            removed = len(todos) - len(kept)
            if removed:
                self._write_locked(kept)
            return removed

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------ internals

    def _ensure_file(self) -> None:
        if self._path.exists():
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(_TEMPLATE, encoding="utf-8")

    def _read_locked(self) -> list[Todo]:
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            self._ensure_file()
            self._cached = []
            try:
                self._mtime_ns = self._path.stat().st_mtime_ns
            except FileNotFoundError:  # pragma: no cover - defensive
                self._mtime_ns = -1
            return self._cached
        if stat.st_mtime_ns == self._mtime_ns:
            return self._cached
        parsed = self._parse_file()
        self._cached = parsed
        self._mtime_ns = stat.st_mtime_ns
        return parsed

    def _parse_file(self) -> list[Todo]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as e:
            _log.warning("todos.read_failed", extra={"path": str(self._path), "error": str(e)})
            return []
        body = raw.partition(_OPEN_HEADER)[2] if _OPEN_HEADER in raw else raw
        todos: list[Todo] = []
        for line in body.splitlines():
            m = _BULLET_RE.match(line)
            if m:
                todos.append(Todo.from_bullet(m.group("mark"), m.group("body")))
        return todos

    def _write_locked(self, todos: list[Todo]) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            raw = _TEMPLATE
        if _OPEN_HEADER in raw:
            preamble = raw.partition(_OPEN_HEADER)[0] + _OPEN_HEADER + "\n\n"
        else:
            preamble = _TEMPLATE
        rendered = preamble + "\n".join(t.render() for t in todos)
        if todos:
            rendered += "\n"
        self._path.write_text(rendered, encoding="utf-8")
        try:
            self._mtime_ns = self._path.stat().st_mtime_ns
        except FileNotFoundError:  # pragma: no cover - defensive
            self._mtime_ns = -1
        self._cached = list(todos)


def default_todos_path(fitt_home_dir: Path) -> Path:
    """``$FITT_HOME/todos.md`` — task state, not identity, so it sits at
    the FITT_HOME root rather than under identity/."""
    return fitt_home_dir / "todos.md"
