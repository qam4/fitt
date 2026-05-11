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
from datetime import UTC
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

    The block also carries a ``[Current time]`` line with the
    gateway's best guess at the operator's local wall-clock time
    plus a UTC offset. Without it, models reason in UTC by
    default and produce naive ISO strings that land in the past
    when interpreted. Observed 2026-05-07: a "remind me at 1 PM"
    request emitted ``at 2026-05-06T13:00:00`` which the cron
    parser read as 13:00 UTC — 8 AM for the user on EDT — and
    fired immediately because 8 AM had already passed.
    """
    tools = registry.list_all()
    lines = [
        _format_current_time_line(),
        "",
        "[Capabilities] You can call these tools:",
    ]
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

    # Trailer: the preamble + tool list above, plus this block,
    # are what the model sees every request. Keep the trailer
    # small but informative. Three parts:
    #
    # 1. How tool approvals actually work. Without this the model
    #    has no mental model for the "I called an ask-bucket
    #    tool and it's not returning yet" case and invents
    #    confirmation rituals ("type 'Approve: X' to proceed")
    #    that don't exist. Observed live 2026-05-07. The approval
    #    middleware routes each ask-bucket call to whichever UI
    #    the client supports; the model's job is just to call
    #    the tool and wait for the result.
    #
    # 2. Honest reporting. The 2026-05-10 Telegram session ended
    #    every model turn with some variant of "You now have a
    #    fully tested, production-grade tool!" regardless of
    #    whether anything actually worked. Success theater
    #    camouflages Problem C (self-deception) — a failed turn
    #    that announces itself as a triumph is harder to catch
    #    than a failed turn that says so. The research (see
    #    hallucinations-and-poisoning.md) says prompting alone
    #    doesn't eliminate this behaviour, but it reduces
    #    magnitude, and costs us nothing if it doesn't help.
    #
    # 3. How to report a missing capability. Unchanged from
    #    Phase 4; the gap log consumes this format.
    trailer_lines = [
        "",
        "[How tool calls work]",
        "Tools in the list above may be `auto` (runs immediately) "
        "or `ask` (pauses for human approval). The approval UI is "
        "surfaced by the client — an inline-keyboard prompt on "
        "Telegram, an approval dialog in the IDE, a terminal "
        "prompt on the CLI. The user taps Approve, Reject, or "
        "Trust session; you never see the UI. After they decide, "
        "the tool either runs and returns its result, or comes "
        "back as a rejection error. Just call the tool and use "
        "the result.",
        "",
        "Do NOT ask the user to confirm by typing a phrase, "
        "reply with a command they should paste, or describe an "
        "approval procedure. The UI handles it. A brief note "
        'like "I\'ll create that cron now" before the tool call '
        "is fine; a full confirmation ritual is not.",
        "",
        "[Honest reporting]",
        "Report what actually happened, including partial "
        "progress and failures. Do not frame incomplete work "
        "as complete, do not celebrate outcomes that haven't "
        "been verified, and do not claim to have done things "
        "you only described. No victory laps. If a tool call "
        "failed or was never made, say so.",
        "",
        "When a request needs a capability not listed above, "
        "reply in this format: \"I'd need a tool to <action>. "
        'Consider adding <suggestion>." We use that to track '
        "which tools to build next.",
    ]
    lines.extend(trailer_lines)
    block = "\n".join(lines)
    if len(block) <= _MAX_BLOCK_CHARS:
        return block
    # Hard cap. Keep the preamble + as many tool lines as fit,
    # then the full trailer so the model always sees both the
    # approval-UX note and the gap-report instruction even when
    # the tool list is what got truncated.
    #
    # Preamble = current-time line + blank + "[Capabilities]..."
    # (first three entries). Keep them together so the context
    # ordering stays predictable.
    preamble = "\n".join(lines[:3])
    trailer = "\n".join(trailer_lines)
    room = _MAX_BLOCK_CHARS - len(preamble) - len(trailer) - 32  # safety
    kept: list[str] = []
    used = 0
    # Tool lines sit between the preamble and the trailer. We
    # computed their count from ``tools`` at the top.
    tool_line_start = 3
    tool_line_stop = tool_line_start + len(tools[:_MAX_TOOLS_IN_BLOCK])
    if len(tools) > _MAX_TOOLS_IN_BLOCK:
        tool_line_stop += 1  # the "... (N more)" note
    for line in lines[tool_line_start:tool_line_stop]:
        if used + len(line) + 1 > room:
            break
        kept.append(line)
        used += len(line) + 1
    kept.append("- ... (capability block truncated; call `list_capabilities` for the full set)")
    return "\n".join([preamble, *kept, trailer])


# --------------------------------------------------------------- current time


def _format_current_time_line() -> str:
    """Render the ``[Current time]`` preamble line.

    The gateway calls ``datetime.now().astimezone()`` to pick up
    whatever tzinfo the container/host provides. In Docker the
    effective zone is the ``TZ`` env var passed in via compose,
    or UTC if unset. That's the same clock the cron scheduler
    reasons in, so the line is truthful.

    Format:
        ``[Current time] Fri 2026-05-08 13:24 EDT (UTC-04:00; 2026-05-08T17:24:00+00:00)``

    Three pieces of information packed in one line: the human-
    readable local wall clock, the local offset, and an explicit
    UTC timestamp. The model uses whichever shape fits its reply
    best; the offset in particular is what lets it emit
    timezone-aware ISO strings to cron_add.
    """
    from datetime import datetime as _dt

    local = _dt.now().astimezone()
    utc = local.astimezone(UTC)
    tz_name = local.tzname() or "local"
    # isoformat without microseconds is more reader-friendly.
    local_wall = local.strftime("%a %Y-%m-%d %H:%M")
    offset = local.strftime("%z")
    # Insert the colon in the offset (e.g. -0400 -> -04:00) to
    # match the ISO-8601 form the model likely emits.
    if offset and len(offset) == 5:
        offset = f"{offset[:3]}:{offset[3:]}"
    utc_iso = utc.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return f"[Current time] {local_wall} {tz_name} (UTC{offset}; {utc_iso})"


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
    action = _tidy(m.group("action") or "")
    if not action:
        return None
    suggestion = _tidy(m.group("suggestion") or "")
    return GapReport(
        ts=ts if ts is not None else time(),
        session_key=session_key,
        action=action,
        suggestion=suggestion,
    )


# --------------------------------------------------------------- narrated tool calls


@dataclass(frozen=True, slots=True)
class NarratedToolCall:
    """A JSON-shaped tool-call that the model emitted in
    ``content`` instead of the API's ``tool_calls`` channel.

    Observed live 2026-05-07 with qwen2.5-coder:14b: cron
    firings produced replies like
    ``I'll call send_message now\\n```json\\n{"name": ..., "arguments": {...}}\\n``` ``
    with no real ``tool_calls`` structure. The agent loop treats
    this as a natural stop (no tool calls, reply complete), the
    cron_runner records it as ``cron_completed.body``, and the
    user gets a JSON dump pushed to their phone — not what they
    asked for.

    We detect the pattern so operators see the failure mode in
    audit/event logs and can decide whether to (a) switch models
    (the operator's choice, in line with "models are configuration"),
    (b) tighten prompting, or (c) treat it as expected for a
    local-only setup where cloud escalation is off the table."""

    tool_name: str
    """The ``name`` field inside the narrated JSON. Empty string
    if the detector matched the shape but couldn't extract a
    name cleanly."""

    raw_fence: str
    """The full fenced block that triggered the match, up to ~500
    chars. Useful when the pattern triggers on a weird emission
    we didn't anticipate."""


# Backtick fence containing a JSON object that has a "name" key
# and (optionally) an "arguments" key. Permissive on whitespace
# and the inner JSON shape — we're looking for the *pattern*,
# not parsing it rigorously. The model doesn't always emit valid
# JSON inside the fence, and a too-strict detector misses the
# exact emissions we want to catch.
_NARRATED_TOOL_RE = re.compile(
    r"""
    ```\s*(?:json)?\s*\n             # opening fence, optional 'json' tag
    (?P<body>
        \{                            # object open
        [^`]*?                        # any non-backtick run (non-greedy)
        "name"\s*:\s*"(?P<name>[^"]+)"
        [^`]*?                        # arguments, etc.
        \}
    )
    \s*\n```                          # closing fence
    """,
    re.VERBOSE | re.DOTALL,
)


def detect_narrated_tool_call(assistant_text: str) -> NarratedToolCall | None:
    """Return a :class:`NarratedToolCall` if ``assistant_text``
    contains a JSON-fenced tool-call-shaped payload; else None.

    Callers should only invoke this when the model's response
    had no actual ``tool_calls`` — a reply that includes BOTH
    a real tool call and a narrated one is the model being
    chatty, not failing. The caller is responsible for that
    precondition; this function looks at text only.

    First match wins. A long reply with multiple narrated calls
    is unusual; we record the first and treat the rest as
    collateral until we see a pattern that demands otherwise.
    """
    if not assistant_text:
        return None
    m = _NARRATED_TOOL_RE.search(assistant_text)
    if m is None:
        return None
    raw = (m.group("body") or "")[:500]
    name = m.group("name") or ""
    return NarratedToolCall(tool_name=name, raw_fence=raw)


def _tidy(s: str) -> str:
    """Clean up one captured phrase.

    The model routinely wraps file names in backticks (e.g.
    ``I'd need a tool to read `README.md`.``), which survive the
    non-greedy ``[^\\n.!?]+?`` capture. Worse, the ``.`` inside
    ``README.md`` terminates the regex early, stranding a lone
    opening backtick in the middle of the captured phrase. The
    ranked output then renders things like ``read the `readme``
    and fails to dedupe against the no-backticks form.

    Backticks are always markdown formatting in this context,
    never action content, so nuke them wholesale. Also collapse
    runs of whitespace that survived the markdown strip."""
    return " ".join(s.replace("`", "").split())


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
