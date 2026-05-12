"""Phase 4.8b — per-turn state machine for the Telegram renderer.

Given a stream of turn events (from the gateway's SSE endpoint
at ``/v1/sessions/<id>/turns/stream``), maintain one
:class:`TurnRenderState` per in-flight turn and drive Telegram
message posts + edits so the user sees the MeshClaw-inspired
growing-bubble UX.

Three bubble types per turn:

1. **Growing stream bubble** — one per turn, lazy-posted on the
   first ``tool_call_planned`` event (or the first reply-token
   via :meth:`append_reply_text`). Contains every tool-call
   status line + the final reply text. Subsequent events edit
   it in place, silently (no notification chime). MeshClaw
   calls this its "task card." Design rule: ONE message per
   turn for the main flow, not per action, to keep scrollback
   readable.

2. **Approval bubbles** — one per ``approval_requested`` event.
   A separate NEW message posted with the inline-keyboard
   (✅ / ❌ / 🔓). Notifies, because blocked approvals need the
   user's attention. On ``approval_decided`` we edit the
   approval bubble in place to show the outcome; we don't
   post a new bubble for the decision. The approval's
   timestamp stays fixed so it doesn't float up past the
   stream bubble on scrollback.

3. **Finish footer** — one per turn, posted on
   ``turn_finished``. Tiny text ("✓ Finished in Ns" /
   "🚫 Rejected" / "⏱️ Timed out"). Notifies, because
   end-of-turn is the phone ping the user wants.

Short-chat-turn detection
-------------------------

A turn that produces NO tool_call_planned AND NO
approval_requested is a plain chat turn. The renderer leaves
its state empty; the chat streaming path in
:mod:`fitt_telegram_bot.handlers` posts the reply as a single
standalone message (today's behaviour). No stream bubble, no
finish footer — the reply message IS the turn's ping.

The class exposes ``should_create_stream_bubble`` so the caller
can decide whether to feed reply-token deltas into the growing
bubble or fall back to the legacy single-message path.

Scope
-----

This module is pure business logic over a :class:`TelegramBot`
protocol. The real python-telegram-bot integration (wrappers
in ``bot.py``, SSE subscriber in ``turn_stream.py``) lives
elsewhere so this file stays unit-testable with an in-memory
stub — same split pattern as :mod:`.handlers`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Protocol

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- constants


MAX_STREAM_BUBBLE_CHARS = 4000
"""Telegram caps message text at 4096 chars. Leave a small
margin for the "(truncated)" suffix. Real turns rarely hit
this; when they do, we stop appending and log a warning (the
JSONL still has the full turn). MeshClaw-style bubble
rotation — posting a new bubble and continuing — is a
post-v1 follow-up."""


MIN_STREAM_EDIT_INTERVAL_S = 1.0
"""Telegram's edit-message rate limit is ~1 edit per second
per chat. Coalesce events arriving faster than this into a
single edit to stay well inside the limit."""


# --------------------------------------------------------------- protocols


class TelegramBot(Protocol):
    """Structural type for the ``python-telegram-bot`` Bot
    operations the renderer needs.

    Methods are async because PTB's are. Real implementations
    are :class:`telegram.Bot`; tests use a recording stub.
    """

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: Any = None,
        disable_notification: bool = False,
    ) -> TelegramMessage: ...

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Any = None,
    ) -> Any: ...


class TelegramMessage(Protocol):
    """The tiny slice of ``telegram.Message`` we use."""

    message_id: int


class InlineKeyboardBuilder(Protocol):
    """Callable that returns a PTB ``InlineKeyboardMarkup`` for
    an approval. Injected so the renderer doesn't import PTB
    directly (keeps unit tests lightweight and lets the real
    wiring reuse the existing callback-data shape)."""

    def __call__(self, approval_id: str) -> Any: ...


# --------------------------------------------------------------- state


@dataclass
class _ToolCallTask:
    """One row in the growing bubble's task list.

    ``placeholder`` is the "🔵 Reading X…" line we posted at
    plan-time. ``final`` gets filled in at execute-time
    ("✅ Read X (12ms)" / "❌ Read X — error"). We keep both
    so the rewriter knows which line to replace."""

    call_id: str
    placeholder: str
    final: str | None = None


class TurnPhase(Enum):
    """Where the turn is in its lifecycle. Used to guard
    against out-of-order events from a buggy producer or a
    resumed SSE stream — a ``tool_call_planned`` arriving
    after ``turn_finished`` is a no-op rather than an
    exception."""

    PENDING = "pending"
    """Turn started; no bubble posted yet."""

    ACTIVE = "active"
    """Stream bubble exists; tool cards accumulating."""

    FINISHED = "finished"
    """turn_finished fired; renderer is done with this turn."""


@dataclass
class TurnRenderState:
    """Everything the renderer knows about one turn.

    One instance per turn_id; lives in memory for the duration
    of the turn (~seconds for a chat turn, up to minutes for
    a cron firing that calls approval-gated tools).
    """

    turn_id: str
    chat_id: int
    phase: TurnPhase = TurnPhase.PENDING

    stream_message_id: int | None = None
    """Telegram message_id of the growing bubble. ``None``
    until the first tool_call_planned / reply-token forces
    a post."""

    tool_tasks: list[_ToolCallTask] = field(default_factory=list)
    """Ordered list of tool-call task cards in the stream
    bubble, in the order they were planned."""

    reply_text: str = ""
    """Accumulated final-reply text. Fed in via
    :meth:`TurnRenderer.append_reply_text` as tokens stream
    in from the chat endpoint."""

    approval_bubbles: dict[str, int] = field(default_factory=dict)
    """approval_id -> Telegram message_id of the approval
    bubble. Populated on ``approval_requested``; looked up
    on ``approval_decided`` to edit the right message."""

    last_stream_edit_ts: float = 0.0
    """``time.monotonic()`` of the last successful stream-bubble
    edit. Used by the coalescer to stay under Telegram's
    rate limit."""

    stream_truncated: bool = False
    """Set when the accumulated stream text crosses the
    length cap. Once set, new appends are no-op'd and a
    warning is logged (once)."""


# --------------------------------------------------------------- renderer


class TurnRenderer:
    """Drive the Telegram UX for one in-flight turn.

    Call :meth:`handle_event` for each SSE frame and
    :meth:`append_reply_text` for each chat-streaming token
    delta. The renderer posts and edits Telegram messages
    accordingly.

    Not thread-safe — intended to be driven from a single
    asyncio task. A separate renderer instance per concurrent
    turn is fine (each holds its own state).
    """

    def __init__(
        self,
        bot: TelegramBot,
        *,
        chat_id: int,
        turn_id: str,
        build_approval_keyboard: InlineKeyboardBuilder,
        clock: Any = None,
    ) -> None:
        self._bot = bot
        self._build_keyboard = build_approval_keyboard
        self._clock = clock or time.monotonic
        self.state = TurnRenderState(turn_id=turn_id, chat_id=chat_id)

    # ------------------------------------------------ public: events

    async def handle_event(self, event: dict[str, Any]) -> None:
        """Process one turn event from the gateway's SSE stream."""
        kind = event.get("kind", "")
        if self.state.phase is TurnPhase.FINISHED:
            # Late event after finish — a ``gap_reported`` that
            # landed just as the loop closed, or an out-of-order
            # frame on a resumed stream. Drop silently; the JSONL
            # still has it, and appending would corrupt the
            # bubble's "done" appearance.
            _log.info(
                "turn_renderer.event_after_finish",
                extra={"turn_id": self.state.turn_id, "kind": kind},
            )
            return

        if kind == "turn_started":
            # No UI side effect; just mark we saw it. The first
            # tool_call_planned / reply-token is what actually
            # forces a bubble post (short-chat-turn detection).
            return
        if kind == "tool_call_planned":
            await self._on_tool_call_planned(event)
        elif kind == "tool_call_executed":
            await self._on_tool_call_executed(event)
        elif kind == "approval_requested":
            await self._on_approval_requested(event)
        elif kind == "approval_decided":
            await self._on_approval_decided(event)
        elif kind == "turn_finished":
            await self._on_turn_finished(event)
        # llm_call_started / llm_call_completed / gap_reported
        # aren't rendered as bubbles in v1. They're on disk and
        # visible via `fitt watch` for the developer view;
        # surfacing them to Telegram would be chatty without
        # adding user value (the task cards + final reply cover
        # the turn story).

    # ------------------------------------------------ public: streaming

    async def append_reply_text(self, delta: str) -> None:
        """Append a chat-streaming token delta to the growing
        bubble's reply portion.

        Called by the chat handler as tokens arrive from
        ``/v1/chat/completions``. Lazy-posts the stream bubble
        if no tool has planned yet (simple chat-only turn with
        no tools but the user still wants streaming — the
        common case). Rate-limited via the coalescer."""
        if not delta or self.state.phase is TurnPhase.FINISHED:
            return
        if self.state.stream_truncated:
            return
        self.state.reply_text += delta
        new_total_len = self._estimated_bubble_len()
        if new_total_len > MAX_STREAM_BUBBLE_CHARS:
            self._mark_stream_truncated()
            return
        await self._ensure_stream_bubble_exists_for_reply()
        await self._flush_stream_bubble_if_due()

    # ------------------------------------------------ public: lifecycle

    def should_create_stream_bubble(self) -> bool:
        """True when the renderer has (or will have) a stream
        bubble for this turn. Callers use this to decide
        whether to route chat-streaming tokens through the
        renderer or the legacy one-message path.

        For v1 the signal is trivial: once any tool has planned,
        the stream bubble is live. A chat-only turn with no
        tool calls returns False initially; the chat handler
        falls back to posting a plain reply message, and the
        renderer's turn_finished is a no-op (no finish footer
        for plain chat — the reply IS the ping)."""
        return self.state.stream_message_id is not None or bool(self.state.tool_tasks)

    async def finalize(self) -> None:
        """Force a final flush of the stream bubble.

        Intended to be called after ``turn_finished`` has
        been processed OR after the chat-streaming path
        closes, whichever is later. The :meth:`handle_event`
        path for ``turn_finished`` handles the finish-footer
        post; this is a belt-and-braces flush of any
        pending append that the coalescer held back."""
        if self.state.stream_message_id is not None and self.state.phase is not TurnPhase.FINISHED:
            await self._edit_stream_bubble()

    # ------------------------------------------------ handlers

    async def _on_tool_call_planned(self, event: dict[str, Any]) -> None:
        meta = event.get("meta") or {}
        tool_name = str(meta.get("tool_name", "?"))
        raw_args = meta.get("args")
        args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
        call_id = str(meta.get("call_id", ""))
        label = _format_tool_action(tool_name, args, phase="planning")
        task = _ToolCallTask(call_id=call_id, placeholder=f"🔵 {label}…")
        self.state.tool_tasks.append(task)
        # Two paths: first tool of a turn lazy-posts a fresh
        # bubble (which already renders this new task row).
        # Subsequent tools force-edit the existing bubble to
        # append their row.
        if self.state.stream_message_id is None:
            await self._ensure_stream_bubble_exists()
        else:
            await self._flush_stream_bubble_if_due(force=True)

    async def _on_tool_call_executed(self, event: dict[str, Any]) -> None:
        meta = event.get("meta") or {}
        call_id = str(meta.get("call_id", ""))
        tool_name = str(meta.get("tool_name", "?"))
        ok = bool(meta.get("ok", False))
        duration_ms = int(meta.get("duration_ms", 0) or 0)
        task = next((t for t in self.state.tool_tasks if t.call_id == call_id), None)
        if task is None:
            # Executed without a matching planned — shouldn't
            # happen but be forgiving. Append a standalone
            # final-state line so the user sees it happened.
            task = _ToolCallTask(call_id=call_id, placeholder=f"🔵 {tool_name}…")
            self.state.tool_tasks.append(task)
        # Reuse the planning-phase label derivation for
        # consistency between planned and executed lines.
        # ``args`` isn't in the executed event, so fall back
        # to the bare tool name.
        verb = _tool_verb(tool_name, phase="done")
        if ok:
            task.final = f"✅ {verb} ({duration_ms}ms)"
        else:
            summary = str(meta.get("result_summary", "")).strip()
            brief = f" — {summary[:80]}" if summary else ""
            task.final = f"❌ {verb}{brief}"
        await self._flush_stream_bubble_if_due(force=True)

    async def _on_approval_requested(self, event: dict[str, Any]) -> None:
        meta = event.get("meta") or {}
        approval_id = str(meta.get("approval_id", ""))
        tool_name = str(meta.get("tool_name", "?"))
        if not approval_id:
            return
        text = (
            f"🔐 Approval needed\n\nTool: `{tool_name}`\n"
            f"Tap a button to decide. Times out if you ignore it."
        )
        try:
            msg = await self._bot.send_message(
                chat_id=self.state.chat_id,
                text=text,
                reply_markup=self._build_keyboard(approval_id),
                disable_notification=False,
            )
            self.state.approval_bubbles[approval_id] = msg.message_id
        except Exception as exc:
            _log.warning(
                "turn_renderer.approval_post_failed",
                extra={
                    "turn_id": self.state.turn_id,
                    "approval_id": approval_id,
                    "error": str(exc),
                },
            )

    async def _on_approval_decided(self, event: dict[str, Any]) -> None:
        meta = event.get("meta") or {}
        approval_id = str(meta.get("approval_id", ""))
        decision = str(meta.get("decision", "?"))
        if not approval_id:
            return
        message_id = self.state.approval_bubbles.get(approval_id)
        if message_id is None:
            # We never posted a bubble for this approval —
            # could be a decide from a different session, or
            # this bot missed the approval_requested event.
            return
        outcome_label = _format_approval_outcome(decision)
        try:
            await self._bot.edit_message_text(
                chat_id=self.state.chat_id,
                message_id=message_id,
                text=f"🔐 Approval {outcome_label}",
                reply_markup=None,
            )
        except Exception as exc:
            _log.warning(
                "turn_renderer.approval_edit_failed",
                extra={
                    "turn_id": self.state.turn_id,
                    "approval_id": approval_id,
                    "error": str(exc),
                },
            )

    async def _on_turn_finished(self, event: dict[str, Any]) -> None:
        meta = event.get("meta") or {}
        status = str(meta.get("status", "ok"))
        iterations = int(meta.get("iterations", 0) or 0)

        # Flush any pending stream-bubble edit first so the
        # final reply tokens land in the bubble before the
        # footer posts — otherwise the footer can arrive
        # before the last edit, out of visual order.
        if self.state.stream_message_id is not None:
            await self._edit_stream_bubble()

        self.state.phase = TurnPhase.FINISHED

        # Short-chat-turn: no tools, no approval, no stream
        # bubble. The chat reply message itself is the ping;
        # skip the footer. This prevents every "thanks" reply
        # from getting a "✓ Finished in 0s" addendum.
        if self.state.stream_message_id is None and not self.state.tool_tasks:
            return

        footer = _format_finish_footer(status, iterations)
        try:
            await self._bot.send_message(
                chat_id=self.state.chat_id,
                text=footer,
                disable_notification=False,
            )
        except Exception as exc:
            _log.warning(
                "turn_renderer.finish_footer_failed",
                extra={
                    "turn_id": self.state.turn_id,
                    "error": str(exc),
                },
            )

    # ------------------------------------------------ stream bubble helpers

    async def _ensure_stream_bubble_exists(self) -> None:
        """Post the stream bubble on first use. Idempotent."""
        if self.state.stream_message_id is not None:
            return
        body = self._compose_stream_bubble_text() or "…"
        try:
            msg = await self._bot.send_message(
                chat_id=self.state.chat_id,
                text=body,
                disable_notification=True,  # silent: this is a live-progress view
            )
            self.state.stream_message_id = msg.message_id
            self.state.phase = TurnPhase.ACTIVE
            self.state.last_stream_edit_ts = self._clock()
        except Exception as exc:
            _log.warning(
                "turn_renderer.stream_bubble_post_failed",
                extra={"turn_id": self.state.turn_id, "error": str(exc)},
            )

    async def _ensure_stream_bubble_exists_for_reply(self) -> None:
        """Same as above but only fires when reply text has
        begun. For short-chat-turns the reply goes to the
        legacy one-message path; once the renderer is in
        charge (tool_tasks non-empty OR stream bubble already
        posted), reply tokens land in the stream bubble.
        The heuristic: only lazy-post for the reply if a
        tool has already planned. A pure-chat turn with no
        tools never opens a stream bubble via this helper."""
        if self.state.tool_tasks and self.state.stream_message_id is None:
            await self._ensure_stream_bubble_exists()

    def _compose_stream_bubble_text(self) -> str:
        """Render the stream bubble's current content.

        Format: tool task lines first, then a blank line, then
        the reply text (if any). Matches MeshClaw's task-card
        + narrative split so the user scans the "what ran"
        rows first and drops into the "what the model said"
        prose after."""
        lines: list[str] = []
        for task in self.state.tool_tasks:
            lines.append(task.final or task.placeholder)
        if self.state.reply_text:
            if lines:
                lines.append("")
            lines.append(self.state.reply_text)
        text = "\n".join(lines)
        if self.state.stream_truncated:
            text += "\n\n(stream bubble truncated at 4 KB; full turn in logs)"
        return text[-MAX_STREAM_BUBBLE_CHARS:]

    async def _flush_stream_bubble_if_due(self, *, force: bool = False) -> None:
        """Issue a Telegram edit if the rate-limit window has
        elapsed, or we're forcing (on a state-changing event
        like tool plan/execute).

        Reply-token appends pass ``force=False`` so a burst of
        deltas coalesces into ~1 edit per second. Tool events
        pass ``force=True`` because they're discrete and
        infrequent enough that edit-per-event is fine."""
        if self.state.stream_message_id is None:
            return
        now = self._clock()
        elapsed = now - self.state.last_stream_edit_ts
        if not force and elapsed < MIN_STREAM_EDIT_INTERVAL_S:
            return
        await self._edit_stream_bubble()

    async def _edit_stream_bubble(self) -> None:
        if self.state.stream_message_id is None:
            return
        body = self._compose_stream_bubble_text()
        if not body:
            return
        try:
            await self._bot.edit_message_text(
                chat_id=self.state.chat_id,
                message_id=self.state.stream_message_id,
                text=body,
            )
            self.state.last_stream_edit_ts = self._clock()
        except Exception as exc:
            # A common failure: "message is not modified" when
            # two events land with identical rendered text.
            # Log at debug and let the caller continue.
            _log.debug(
                "turn_renderer.stream_bubble_edit_failed",
                extra={"turn_id": self.state.turn_id, "error": str(exc)},
            )

    def _estimated_bubble_len(self) -> int:
        """Cheap-ish size estimate without composing the full
        string on every append. Used to short-circuit the
        4 KB overflow check."""
        task_len = sum(len(t.final or t.placeholder) + 1 for t in self.state.tool_tasks)
        return task_len + len(self.state.reply_text) + 4  # +4 for separators

    def _mark_stream_truncated(self) -> None:
        if self.state.stream_truncated:
            return
        self.state.stream_truncated = True
        _log.warning(
            "turn_renderer.stream_truncated",
            extra={"turn_id": self.state.turn_id},
        )


# --------------------------------------------------------------- formatting


_KNOWN_TOOL_VERBS: dict[str, tuple[str, str]] = {
    "read_file": ("Reading", "Read"),
    "write_file": ("Writing", "Wrote"),
    "edit_file": ("Editing", "Edited"),
    "git_status": ("Checking git status", "Checked git status"),
    "git_diff": ("Reading git diff", "Read git diff"),
    "git_commit": ("Committing", "Committed"),
    "git_push": ("Pushing", "Pushed"),
    "git_pull": ("Pulling", "Pulled"),
    "run_tests": ("Running tests", "Ran tests"),
    "project_shell": ("Running shell", "Ran shell"),
    "cron_add": ("Creating cron", "Created cron"),
    "cron_list": ("Listing crons", "Listed crons"),
    "cron_remove": ("Removing cron", "Removed cron"),
    "send_message": ("Sending message", "Sent message"),
    "learn_lesson": ("Recording lesson", "Recorded lesson"),
}
"""Human-readable (planning, done) verb pairs for common tool
names. Falls back to using the tool name itself when
unregistered (rare — the block above covers every inline
tool; MCP tools can grow entries here as they ship)."""


def _tool_verb(name: str, *, phase: Literal["planning", "done"]) -> str:
    """Return a human verb for ``name`` in the given phase,
    falling back to the tool name for unknowns."""
    pair = _KNOWN_TOOL_VERBS.get(name)
    if pair is None:
        return f"{'Running' if phase == 'planning' else 'Ran'} `{name}`"
    return pair[0] if phase == "planning" else pair[1]


def _format_tool_action(
    name: str, args: dict[str, Any], *, phase: Literal["planning", "done"]
) -> str:
    """One line for the task card in the stream bubble.

    Short and grep-friendly: verb + primary-argument-hint.
    "Reading README.md", "Running shell `git status`",
    "Committing" (when no argument is distinctive).
    """
    verb = _tool_verb(name, phase=phase)
    hint = _primary_arg_hint(name, args)
    return f"{verb} {hint}" if hint else verb


def _primary_arg_hint(name: str, args: dict[str, Any]) -> str:
    """Pull the most-telling single argument value out of an
    args dict for the task-card line.

    Keeps the format stable across tools without maintaining
    a registry: path-like args, command args, and cron names
    cover ~everything."""
    if not args:
        return ""
    for key in ("path", "file", "command", "message", "name", "session_key", "alias"):
        v = args.get(key)
        if isinstance(v, str) and v:
            return f"`{_truncate(v, 60)}`"
    return ""


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)] + "…"


def _format_approval_outcome(decision: str) -> str:
    """Map the gateway's decision literal to a bubble label."""
    if decision == "approve":
        return "✅ approved"
    if decision == "trust_session":
        return "🔓 trusted for session"
    if decision == "reject":
        return "❌ rejected"
    if decision == "timeout":
        return "⏱️ timed out"
    if decision == "denied_deny_list":
        return "🚫 denied by policy"
    return f"({decision})"


def _format_finish_footer(status: str, iterations: int) -> str:
    """Tiny end-of-turn footer. Matches MeshClaw's ``Finished
    in Ns`` but without the elapsed time (we don't have a
    wall-clock start in the state; the ``turn_finished``
    event's ``duration`` field would give it, and a future
    iteration can surface that). For v1 the status literal
    + iteration count is enough of a ping."""
    if status == "ok":
        return f"✓ Finished in {iterations} step" + ("s" if iterations != 1 else "")
    if status == "tool_loop_exhausted":
        return "⏱️ Turn exhausted tool-loop budget"
    if status == "upstream_error":
        return "🚫 Turn failed (upstream error)"
    return f"• Finished ({status})"


# --------------------------------------------------------------- exports


__all__ = [
    "MAX_STREAM_BUBBLE_CHARS",
    "MIN_STREAM_EDIT_INTERVAL_S",
    "InlineKeyboardBuilder",
    "TelegramBot",
    "TelegramMessage",
    "TurnPhase",
    "TurnRenderState",
    "TurnRenderer",
]
