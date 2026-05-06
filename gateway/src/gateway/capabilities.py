"""Capability block + gap logging.

Two halves:

* **The block.** A short section injected into every chat
  request's system prompt, listing the tools FITT currently
  exposes. Keeps the model anchored on what it *can* do so it
  stops hallucinating tool names or silently giving up when a
  tool would have worked.

* **The gap log.** When the model says "I'd need a tool to do X"
  in a standard format, we append one line to
  ``$FITT_HOME/capability_gaps.log``. Over time this builds a
  ranked list of missing-capability complaints — the natural
  backlog for which tool to add next.

Gap-detection shape
-------------------

We prompt the model to reply with this exact phrasing when it
can't do what was asked::

    I'd need a tool to <action>. Consider adding <suggestion>.

The regex is permissive: the "Consider adding" half is optional,
apostrophe vs straight quote doesn't matter, and we accept
either 'I would need' or 'I need'. We prefer false-negatives (we
miss some gap reports) over false-positives (we record the
wrong thing as a gap) — the whole point of the log is to be
trustworthy enough that "what should I build next" can be
answered by grepping it.

See Principle 8 in the roadmap: the agent is honest about its
capabilities. This module is that principle made executable.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from time import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .tools import ToolRegistry

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- block


_MAX_TOOLS_IN_BLOCK = 40
"""Enough room for the full inline set plus a reasonable number
of MCP tools, but capped so a badly-configured MCP server that
surfaces hundreds of tools doesn't overwhelm the system prompt.
If we go over, we keep the first N and note the truncation."""

_MAX_BLOCK_CHARS = 4_000
"""Size cap on the whole capability block. Matches the 6 KB
history budget's order of magnitude so the system prompt can't
swallow the entire context window. Truncation notes point at
``list_capabilities()`` for the model to query the full set."""


def build_capability_block(registry: ToolRegistry) -> str:
    """Return the ``[Capabilities]`` block for the system prompt.

    Tools are listed in the order ``registry.list_all()`` returns
    them (inline tools first, then MCP, alphabetically within
    each bucket). Each line is ``- `name`: description``; we
    intentionally keep the shape minimal because the model
    already knows its own tool-calling API — it just needs to
    know *which names are live*.
    """
    tools = registry.list_all()
    lines = ["[Capabilities] You can call these tools:"]
    if not tools:
        lines.append("- (no tools registered)")
    else:
        shown = tools[:_MAX_TOOLS_IN_BLOCK]
        for t in shown:
            lines.append(f"- `{t.name}`: {t.description}")
        if len(tools) > _MAX_TOOLS_IN_BLOCK:
            lines.append(
                f"- ... ({len(tools) - _MAX_TOOLS_IN_BLOCK} more; "
                f"call `list_capabilities` for the full set)"
            )
    lines.append("")
    lines.append(
        "When a request needs a capability not listed above, "
        "reply in this format: \"I'd need a tool to <action>. "
        'Consider adding <suggestion>." We use that to track '
        "which tools to build next."
    )
    block = "\n".join(lines)
    if len(block) <= _MAX_BLOCK_CHARS:
        return block
    # Hard cap. Keep the preamble + as many tool lines as fit,
    # then a truncation note.
    preamble = lines[0]
    trailer = "\n".join(lines[-2:])  # blank line + instruction
    room = _MAX_BLOCK_CHARS - len(preamble) - len(trailer) - 32  # safety
    kept: list[str] = []
    used = 0
    for line in lines[1:-2]:
        if used + len(line) + 1 > room:
            break
        kept.append(line)
        used += len(line) + 1
    kept.append("- ... (capability block truncated; call `list_capabilities` for the full set)")
    return "\n".join([preamble, *kept, trailer])


# --------------------------------------------------------------- gap report


@dataclass(frozen=True, slots=True)
class GapReport:
    """One parsed capability-gap statement from a model reply."""

    ts: float
    session_key: str
    action: str
    """What the model said it'd need to do (the ``<action>``
    phrase). Trimmed, single-line."""

    suggestion: str
    """What the model suggested adding. Empty string when the
    reply used only the short form without the ``Consider
    adding`` half."""


_GAP_RE = re.compile(
    r"""
    (?:^|\W)
    I[' \u2019]?\s*(?:d|would)\s+need\s+a\s+tool\s+to\s+
    (?P<action>[^\n.!?]+?)
    \s*[.!?]
    (?:
        \s*
        Consider\s+adding\s+
        (?P<suggestion>[^\n.!?]+?)
        \s*[.!?]
    )?
    """,
    re.IGNORECASE | re.VERBOSE,
)
"""Match the standard gap phrasing. Anchor with a non-word
prefix so we don't over-match an arbitrary sentence containing
the phrase as a fragment. Both halves are non-greedy; the full
stop terminator keeps us inside one sentence."""


def parse_gap(
    reply: str, *, session_key: str = "main", ts: float | None = None
) -> GapReport | None:
    """Return a GapReport if ``reply`` contains the standard gap
    phrasing; else None.

    Matches are returned for the first occurrence only. A single
    reply complaining about two missing tools is a spec we can
    add later — for now, the model's own convention is one gap
    per reply."""
    if not reply:
        return None
    m = _GAP_RE.search(reply)
    if m is None:
        return None
    action = (m.group("action") or "").strip()
    if not action:
        return None
    suggestion = (m.group("suggestion") or "").strip()
    return GapReport(
        ts=ts if ts is not None else time(),
        session_key=session_key,
        action=action,
        suggestion=suggestion,
    )


# --------------------------------------------------------------- log


class CapabilityGapLog:
    """Append-only JSONL record of capability gaps.

    Lives at ``$FITT_HOME/capability_gaps.log`` by default. One
    line per gap; same append discipline as the audit log but no
    HMAC — gaps aren't adversarial, they're just a TODO list."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def append(self, gap: GapReport) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(asdict(gap), ensure_ascii=False) + "\n"
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line)

    def read(self, since: float | None = None) -> list[GapReport]:
        """Parse the log into GapReports. Malformed lines are
        dropped with a warning."""
        if not self._path.exists():
            return []
        out: list[GapReport] = []
        with self._path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    _log.warning(
                        "capability_gaps.malformed",
                        extra={"line_sample": line[:120]},
                    )
                    continue
                try:
                    gap = GapReport(
                        ts=float(data["ts"]),
                        session_key=str(data["session_key"]),
                        action=str(data["action"]),
                        suggestion=str(data.get("suggestion", "")),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if since is not None and gap.ts < since:
                    continue
                out.append(gap)
        return out


def rank_gaps(gaps: list[GapReport]) -> list[tuple[str, int, GapReport]]:
    """Group gaps by a canonicalised action string and return
    ``[(action, count, most_recent_gap), ...]`` ordered by count
    descending.

    Canonicalisation lower-cases and collapses whitespace so
    "fetch a webpage" and "Fetch  a webpage" dedupe. We
    deliberately don't stem or aggressively normalise — the
    operator reads this list and decides what to build, not a
    machine."""
    groups: dict[str, list[GapReport]] = {}
    for g in gaps:
        key = _canonicalise(g.action)
        groups.setdefault(key, []).append(g)
    ranked = sorted(
        ((k, len(vs), max(vs, key=lambda g: g.ts)) for k, vs in groups.items()),
        key=lambda row: (-row[1], -row[2].ts),
    )
    return ranked


def _canonicalise(s: str) -> str:
    return " ".join(s.lower().split())


def default_gap_log_path(fitt_home: Path) -> Path:
    return fitt_home / "capability_gaps.log"
