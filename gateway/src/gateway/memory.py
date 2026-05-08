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
from typing import Any

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

    history_messages: list[dict[str, Any]]
    """List of messages for the history turns that fit in the
    budget, oldest first. Shape follows the OpenAI chat-messages
    contract: most entries are ``{"role": "user|assistant",
    "content": str}``; tool-using turns (Phase 5) also emit
    ``{"role": "assistant", "content": "", "tool_calls": [...]}``
    and ``{"role": "tool", "tool_call_id": str, "content": str}``
    entries. Empty if no history or memory is disabled."""

    truncated_bytes: int = 0
    """How many bytes of history were dropped due to the budget.
    0 if nothing was truncated."""


@dataclass
class _Turn:
    role: str
    content: str
    timestamp: datetime | None = None


@dataclass(frozen=True, slots=True)
class PersistedToolCall:
    """Phase 5 — one tool call persisted with its result.

    Written to the on-disk history as a bullet under an
    ``assistant tool_calls`` header, followed by a
    ``tool <name>`` block carrying ``result_status`` +
    ``result_summary``.

    Kept short by design — ``args_summary`` caps at ~80 chars,
    ``result_summary`` at ~300 chars. The point is to preserve
    the OUTCOME of the call (so tomorrow's context knows the
    SSH call succeeded) without ballooning context windows
    with full stdout.
    """

    tool_name: str
    args_summary: str
    result_status: str
    """Short status: ``ok`` on success; ``exit=N`` for shell
    tools that returned a non-zero exit; ``error`` for other
    failures. Parsed back out when reloading."""
    result_summary: str
    """Body of the tool result. For success this is whatever
    the caller chose (e.g. ``"wrote a.txt"``); for errors the
    first ~300 chars of the error text. Never the full
    stdout — that's too much for tomorrow's context."""

    def render_call_bullet(self) -> str:
        """One line for the ``assistant tool_calls`` block."""
        args = self.args_summary
        if len(args) > 80:
            args = args[:77] + "..."
        return f"- {self.tool_name}({args})"

    def render_result_body(self) -> str:
        """Body content for the corresponding ``tool <name>``
        block. Just the status + summary, joined with a colon
        when the summary is non-empty."""
        summary = self.result_summary.strip()
        if not summary:
            return self.result_status
        if summary == self.result_status:
            return self.result_status
        return f"{self.result_status}: {summary}"


# ----------------------------------------------------------------- store


_HEADER_RE = re.compile(
    # Matches any of:
    #   ## <ts> user
    #   ## <ts> assistant
    #   ## <ts> assistant tool_calls     (Phase 5 — tool-using turn)
    #   ## <ts> tool <tool_name>         (Phase 5 — tool result block)
    #   ## <ts> system
    # The role is captured as a single string including any suffix
    # (e.g. "assistant tool_calls" or "tool project_shell") so the
    # parser dispatches downstream. Unknown roles degrade to the
    # pre-Phase-5 behaviour: the block is treated as a turn with
    # the literal role, callers decide what to do with it.
    r"^##\s+(?P<ts>\S+)\s+(?P<role>user|assistant(?:\s+tool_calls)?|"
    r"tool(?:\s+\S+)?|system)\s*$"
)


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
        *,
        tool_calls: list[PersistedToolCall] | None = None,
    ) -> None:
        """Append a user/assistant pair to today's history.

        Atomic from the reader's perspective: either the full pair is
        present or neither half is. We use a single ``write_text``
        operation on a buffered string rather than two appends, which
        means a process crash mid-write loses the pair rather than
        leaving a half-written user block.

        Phase 5: ``tool_calls`` (optional) lets chat/cron handlers
        persist the structured outcome of a tool-using turn. When
        present, the turn writes:

            ## <ts> user              <user_message>
            ## <ts> assistant tool_calls
                - tool(args)...  per PersistedToolCall
            ## <ts> tool <name>       <status + summary>  (one per call)
            ## <ts> assistant         <assistant_message>

        So a future request loading this day sees the call went
        through AND what happened — not just the paraphrased
        natural-language reply. This is the fix for the
        tool-poisoning failure mode documented in
        ``test_session_poisoning_lifecycle`` (Phase 4.6).

        When ``tool_calls`` is ``None`` or empty, the format is
        unchanged from Phase 2 (just user + assistant). That's
        the majority path.
        """
        if not self._enabled:
            return

        path = self.history_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        now = _utc_now()
        user_block = _format_block(now, "user", user_message)
        blocks: list[str] = [user_block]

        if tool_calls:
            calls_body = "\n".join(c.render_call_bullet() for c in tool_calls)
            blocks.append(_format_block(now, "assistant tool_calls", calls_body))
            for call in tool_calls:
                blocks.append(
                    _format_block(now, f"tool {call.tool_name}", call.render_result_body())
                )

        blocks.append(_format_block(now, "assistant", assistant_message))

        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        # Ensure exactly one blank line between the previous turn and
        # the new one.
        separator = "\n" if existing and not existing.endswith("\n\n") else ""
        new_content = existing + separator + "".join(blocks)
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

    def _load_and_truncate_history(self, path: Path) -> tuple[list[dict[str, Any]], int]:
        """Read a history file, parse into turns, drop oldest turns
        until the total content size fits the budget, and convert
        the remaining turns into an OpenAI-shape message list.

        Returns (messages, dropped_bytes). Missing or unreadable
        files are treated as empty and do not raise.

        Phase 5: a tool-using turn on disk is represented as four
        blocks (user, assistant tool_calls, tool <name>+, final
        assistant). They're converted back into the same
        OpenAI-shape ``role=assistant + tool_calls`` / ``role=tool
        + tool_call_id`` pair the live tool loop emits. That way
        a model reloading history sees the OUTCOME of a prior
        tool call, not just the paraphrased natural-language
        reply — which was the tool-poisoning failure mode the
        session-poisoning e2e test documents.
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

        messages = _turns_to_messages(turns)
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
            timestamp_str, role = m.group("ts"), m.group("role")
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


# Matches a single call bullet inside an ``assistant tool_calls``
# block: "- tool_name(args...)". Captures the tool name and the
# args text so we can reconstruct an OpenAI-shape ``tool_calls``
# entry when loading.
_TOOL_CALL_BULLET_RE = re.compile(
    r"^\s*-\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"\s*\((?P<args>.*)\)\s*$"
)


def _turns_to_messages(turns: list[_Turn]) -> list[dict[str, Any]]:
    """Convert parsed turns into OpenAI-shape message dicts.

    Phase 5: a tool-using turn is represented on disk as
    ``user`` → ``assistant tool_calls`` → one or more
    ``tool <name>`` → final ``assistant``. Convert that shape
    into the LLM contract:

        [
            {"role": "user", ...},
            {"role": "assistant", "tool_calls": [{...}], "content": ""},
            {"role": "tool", "tool_call_id": "...", "content": "..."},
            {"role": "assistant", "content": "final reply"},
        ]

    Tool-call ids are derived deterministically from the bullet
    text so the paired ``role: tool`` entries get valid ids.
    The model doesn't see ids as semantic; it just needs them
    to be valid strings that correspond across the assistant /
    tool message pair.

    Phase-2-shape files (only user/assistant turns) load
    identically to today — back-compat is the whole reason the
    conversion is per-turn-role rather than a global rewrite.
    Unknown-role turns are skipped with a debug log.
    """
    messages: list[dict[str, Any]] = []
    i = 0
    while i < len(turns):
        turn = turns[i]
        role = turn.role
        if role == "user":
            messages.append({"role": "user", "content": turn.content})
            i += 1
            continue
        if role == "assistant":
            messages.append({"role": "assistant", "content": turn.content})
            i += 1
            continue
        if role == "assistant tool_calls":
            # Parse call bullets; associate each with the next
            # ``tool <name>`` block in order.
            tool_calls, tool_results = _parse_tool_calls_block(turn.content)
            # Look ahead for the matching tool blocks.
            result_idx = 0
            peek = i + 1
            while (
                peek < len(turns)
                and turns[peek].role.startswith("tool ")
                and result_idx < len(tool_calls)
            ):
                tool_turn = turns[peek]
                # Override the parsed-from-bullet result with the
                # real block content (the bullet only carries args;
                # the actual result lives in the tool block body).
                tool_results[result_idx] = {
                    "tool_call_id": tool_calls[result_idx]["id"],
                    "content": tool_turn.content,
                }
                peek += 1
                result_idx += 1
            # Emit assistant with tool_calls, then a role=tool per call.
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": tool_calls,
                }
            )
            for result in tool_results:
                messages.append({"role": "tool", **result})
            i = peek
            continue
        if role.startswith("tool "):
            # Orphan tool block (no preceding assistant tool_calls).
            # Happens if the parser hit a malformed file; skip
            # with a debug log rather than raise.
            _log.debug(
                "memory.orphan_tool_block",
                extra={"role": role, "content_head": turn.content[:80]},
            )
            i += 1
            continue
        if role == "system":
            # We persist system messages rarely (future uses),
            # but if one appears, inject it as-is.
            messages.append({"role": "system", "content": turn.content})
            i += 1
            continue
        # Unknown role — drop with a debug log.
        _log.debug(
            "memory.unknown_role_skipped",
            extra={"role": role, "content_head": turn.content[:80]},
        )
        i += 1
    return messages


def _parse_tool_calls_block(
    body: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse the body of an ``assistant tool_calls`` block into
    a list of tool_calls dicts (for the assistant message) and
    a matching list of result-placeholder dicts (filled in by
    the caller from the following ``tool <name>`` blocks).

    Returns ``(tool_calls, result_placeholders)``. The ids are
    deterministic hashes of the bullet text so the two halves
    can pair without the on-disk format needing explicit ids.
    """
    import hashlib
    import json

    tool_calls: list[dict[str, Any]] = []
    result_placeholders: list[dict[str, Any]] = []
    for idx, line in enumerate(body.splitlines()):
        m = _TOOL_CALL_BULLET_RE.match(line)
        if not m:
            continue
        name = m.group("name")
        args_text = m.group("args").strip()
        call_id = "persisted-" + hashlib.sha1(line.encode("utf-8")).hexdigest()[:12]
        tool_calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    # Preserve the args literally — the model
                    # will see the bullet's args text, same as
                    # what was rendered in the live call. The
                    # function.arguments field is a JSON string
                    # per the OpenAI contract; wrap in a
                    # single-field object so the shape validates.
                    "name": name,
                    "arguments": json.dumps({"_persisted_args": args_text}),
                },
            }
        )
        result_placeholders.append({"tool_call_id": call_id, "content": ""})
        _ = idx
    return tool_calls, result_placeholders
