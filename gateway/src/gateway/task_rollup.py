"""Cross-phase task rollup - a derived view over the per-phase spec
task lists (``.kiro/specs/<phase>/tasks.md``).

Why
---

The specs are the single source of truth for per-phase work, and each
``tasks.md`` already reads like a little kanban (``- [ ]`` / ``- [x]``).
The friction is that there are ~18 of them plus a roadmap, so "what's
open across everything" means opening every file. This module reads them
so the answer is one ``fitt tasks`` glance - generated on demand, never
hand-maintained, so it can't drift out of sync with the specs.

This is the **pure layer**: parsing + classification over text handed in
by the caller. The ``fitt tasks`` CLI is the thin shell that finds the
files and renders. No state, no I/O beyond the small ``collect_tasks``
filesystem walk, fully unit-testable.

Classification
--------------

An open ``- [ ]`` task isn't automatically "ready dev work". The spec
convention deliberately leaves some boxes unchecked forever:

* **at-home / manual** - runtime steps only the operator can do at home
  (``netstat`` check, external port scan, reboot test, manual project
  registration). Tagged so the rollup doesn't present them as pickable.
* **deferred / pilot / reshaped** - punted to a later milestone or
  shelved (e.g. the reshaped spec-runner phase).

Everything else open is genuine ``open`` work. The markers are simple
substring heuristics over each task's text block; they err toward
leaving an item in ``open`` (a false "ready" is cheaper than hiding real
work).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

TaskState = Literal["open", "done"]
TaskKind = Literal["open", "at_home", "deferred", "done"]

# Top-level checkbox bullets, column 0: ``- [ ] 12. Title`` or
# ``- [ ] 8c. Title``. Indented sub-bullets are intentionally skipped so
# a task and its sub-points aren't double-counted.
_CHECKBOX_RE = re.compile(r"^- \[(?P<mark>[ xX])\]\s+(?P<rest>.+?)\s*$")
_ID_RE = re.compile(r"^(?P<id>[0-9]+[a-z]?)\.\s+(?P<title>.+)$")

# An open task's text block matching one of these (case-insensitive) is
# reclassified away from "ready dev work".
_AT_HOME_MARKERS = (
    "at-home",
    "at home",
    "author only",
    "(manual",
    "manual,",
    "port scan",
    "reboot",
    "runtime)",
)
_DEFERRED_MARKERS = (
    "deferred to pilot",
    "deferred to a follow",
    "deferred until",
    "reshaped",
    "(deferred",
)

# Phase-level status, declared by a ``**Status:** <word>`` line near the
# top of each spec's tasks.md. The rollup uses it to collapse phases
# whose boxes are historical (shipped/shelved) so they don't drown out
# the genuinely-actionable ones, instead of requiring ~200 stale
# checkboxes to be hand-ticked.
PhaseStatus = Literal["active", "blocked", "shipped", "shelved"]
VALID_STATUSES: tuple[PhaseStatus, ...] = ("active", "blocked", "shipped", "shelved")
DEFAULT_STATUS: PhaseStatus = "active"
# A phase in one of these is done/abandoned: its open boxes are
# historical and the rollup collapses rather than enumerates them.
COLLAPSED_STATUSES: frozenset[str] = frozenset({"shipped", "shelved"})

_STATUS_RE = re.compile(
    r"^\s*>?\s*\*{0,2}status\*{0,2}\s*[:=]\s*\*{0,2}\s*(?P<status>[a-z]+)\s*\*{0,2}\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Task:
    """One parsed checkbox task from a spec ``tasks.md``."""

    phase: str
    id: str
    title: str
    state: TaskState
    kind: TaskKind


def classify_open(text_blob: str) -> TaskKind:
    """Classify an *open* task's text block into ``open`` / ``at_home`` /
    ``deferred``. Deferred wins over at-home when both match (a shelved
    item is more "not now" than a runtime chore)."""
    low = text_blob.lower()
    if any(m in low for m in _DEFERRED_MARKERS):
        return "deferred"
    if any(m in low for m in _AT_HOME_MARKERS):
        return "at_home"
    return "open"


def parse_tasks_md(phase: str, content: str) -> list[Task]:
    """Parse one ``tasks.md`` into a list of :class:`Task`.

    Captures each top-level checkbox bullet plus its continuation lines
    (indented text and sub-bullets up to the next top-level bullet or
    header) so the classifier can read markers like "(At-home, runtime.)"
    that sit on a follow-on line."""
    lines = content.splitlines()
    tasks: list[Task] = []
    i = 0
    n = len(lines)
    while i < n:
        m = _CHECKBOX_RE.match(lines[i])
        if m is None:
            i += 1
            continue
        rest = m.group("rest")
        id_m = _ID_RE.match(rest)
        if id_m is not None:
            tid = id_m.group("id")
            title = id_m.group("title")
        else:
            tid = "-"
            title = rest

        # Gather continuation lines for marker classification.
        blob = [rest]
        j = i + 1
        while j < n:
            nxt = lines[j]
            if _CHECKBOX_RE.match(nxt) or nxt.startswith("#"):
                break
            blob.append(nxt)
            j += 1

        state: TaskState = "done" if m.group("mark").lower() == "x" else "open"
        kind: TaskKind = "done" if state == "done" else classify_open("\n".join(blob))
        tasks.append(Task(phase=phase, id=tid, title=title, state=state, kind=kind))
        i = j
    return tasks


def parse_status(content: str) -> PhaseStatus:
    """Read the ``**Status:** <word>`` line near the top of a tasks.md.

    Tolerant of ``> Status: shipped`` / ``**Status:** shipped`` /
    ``Status = active``. Scans only the first 40 lines (the status
    belongs at the top, by the title). Returns :data:`DEFAULT_STATUS`
    (``active``) when absent or unrecognised - a spec with no marker is
    assumed to still be in flight, so its open work shows."""
    for line in content.splitlines()[:40]:
        m = _STATUS_RE.match(line)
        if m is not None:
            word = m.group("status").lower()
            for status in VALID_STATUSES:
                if word == status:
                    return status
    return DEFAULT_STATUS


def find_specs_dir(start: Path) -> Path | None:
    """Walk up from ``start`` (inclusive) looking for ``.kiro/specs``.

    Lets ``fitt tasks`` work whether it's run from the repo root, from
    ``gateway/``, or any subdir - no ``--specs-dir`` needed in the common
    case."""
    for parent in [start, *start.parents]:
        candidate = parent / ".kiro" / "specs"
        if candidate.is_dir():
            return candidate
    return None


def collect_tasks(specs_dir: Path) -> list[Task]:
    """Parse every ``<phase>/tasks.md`` under ``specs_dir``, sorted by
    phase directory name."""
    out: list[Task] = []
    for tasks_md in sorted(specs_dir.glob("*/tasks.md")):
        phase = tasks_md.parent.name
        try:
            content = tasks_md.read_text(encoding="utf-8")
        except OSError:
            continue
        out.extend(parse_tasks_md(phase, content))
    return out


def collect_statuses(specs_dir: Path) -> dict[str, PhaseStatus]:
    """Map each phase directory name to its declared :data:`PhaseStatus`
    (``active`` when no ``**Status:**`` line is present)."""
    out: dict[str, PhaseStatus] = {}
    for tasks_md in sorted(specs_dir.glob("*/tasks.md")):
        try:
            content = tasks_md.read_text(encoding="utf-8")
        except OSError:
            continue
        out[tasks_md.parent.name] = parse_status(content)
    return out


@dataclass(frozen=True, slots=True)
class PhaseRollup:
    """Per-phase counts + the genuinely-open tasks for that phase."""

    phase: str
    open_tasks: list[Task]
    at_home: int
    deferred: int
    done: int
    status: PhaseStatus = DEFAULT_STATUS

    @property
    def total(self) -> int:
        return len(self.open_tasks) + self.at_home + self.deferred + self.done

    @property
    def collapsed(self) -> bool:
        """True when the phase is shipped/shelved - its open boxes are
        historical, so the rollup summarises rather than lists them."""
        return self.status in COLLAPSED_STATUSES

    @property
    def actionable_open(self) -> int:
        """Open tasks that count toward "what's next" - zero for a
        collapsed (shipped/shelved) phase."""
        return 0 if self.collapsed else len(self.open_tasks)


def roll_up(tasks: list[Task], statuses: dict[str, PhaseStatus] | None = None) -> list[PhaseRollup]:
    """Group ``tasks`` by phase into :class:`PhaseRollup`s.

    ``statuses`` maps phase -> declared status (from
    :func:`collect_statuses`); omitted phases default to ``active``.
    Ordering leads with phases that have *actionable* open work
    (active/blocked with open tasks), then collapsed/empty phases by
    name - so the rollup opens on what's pickable."""
    statuses = statuses or {}
    by_phase: dict[str, list[Task]] = {}
    for t in tasks:
        by_phase.setdefault(t.phase, []).append(t)

    rollups: list[PhaseRollup] = []
    for phase, items in by_phase.items():
        rollups.append(
            PhaseRollup(
                phase=phase,
                open_tasks=[t for t in items if t.kind == "open"],
                at_home=sum(1 for t in items if t.kind == "at_home"),
                deferred=sum(1 for t in items if t.kind == "deferred"),
                done=sum(1 for t in items if t.kind == "done"),
                status=statuses.get(phase, DEFAULT_STATUS),
            )
        )
    rollups.sort(key=lambda r: (r.actionable_open == 0, r.phase))
    return rollups
