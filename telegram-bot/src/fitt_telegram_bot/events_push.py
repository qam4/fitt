"""Telegram-side formatters for FITT event-log entries.

Phase 4.5 scaffolding. Task 5.5e adds the two late-tool formats
(``late_tool_result`` / ``late_tool_rejected``); Task 7 wires the
actual push subscriber that consumes events and delivers messages
over this formatting surface.

The formatters are pure functions over dict shapes — they don't
need the gateway or PTB in scope. That keeps this module cheap to
import from tests and keeps the hot path (subscriber → format →
``bot.send_message``) obvious once Task 7 lands.

Event dict shape (mirrors ``gateway.events.EventEntry`` when
serialised):

.. code-block:: python

    {
        "ts": 1759929000.0,
        "kind": "late_tool_result",
        "session_key": "main",
        "title": "✅ late result: write_file",
        "body": "wrote a.txt; done.",
        "meta": {
            "tool": "write_file",
            "approval_id": "u-uuid",
            "original_session_key": "main",
            "original_client": "telegram",
            "status": "ok",
            "iterations": 2,
        },
    }

Body cap matches ``events.telegram_body_cap`` in the gateway's
config (default 3500). Overflow gets replaced with a trailing
note so the user knows the message was truncated rather than
silently lost.
"""

from __future__ import annotations

from typing import Any

# Has to match ``events.telegram_body_cap`` in the gateway config.
# Hardcoded here rather than read from config because the bot and
# gateway live in separate processes; if an operator bumps the cap
# in config.yaml they should bump it here too. Documented in the
# bot README.
_BODY_CAP = 3500
_TRUNCATED = "\n\n... (truncated)"


def format_event(entry: dict[str, Any]) -> str:
    """Render an event as a Telegram message body.

    Dispatches on ``kind`` to a per-kind formatter. Unknown kinds
    fall back to ``title`` + ``body`` so a new event type added
    upstream surfaces *something* instead of being silently
    dropped.
    """
    kind = entry.get("kind", "")
    body = _cap(entry.get("body", ""))
    title = entry.get("title", "")

    if kind == "late_tool_result":
        return _format_late_tool_result(entry, body)
    if kind == "late_tool_rejected":
        return _format_late_tool_rejected(entry, body)
    if kind == "cron_completed":
        return _format_cron_completed(entry, body)
    if kind == "cron_failed":
        return _format_cron_failed(entry, body)
    if kind == "agent_message":
        return _format_agent_message(entry, body)
    if kind == "tool_executed":
        return _format_tool_executed(entry, body)

    # Fallback for unknown kinds.
    return _join_non_empty(title or kind, body)


# ---------------------------------------------------- per-kind formatters


def _format_late_tool_result(entry: dict[str, Any], body: str) -> str:
    """A tool was approved after the chat turn had already
    detached. The user sees the model's final reply as the body
    so the late message reads like a continuation of the
    conversation, not a system notice."""
    tool = entry.get("meta", {}).get("tool") or "tool"
    header = f"✅ Late result from {tool}"
    return _join_non_empty(header, body)


def _format_late_tool_rejected(entry: dict[str, Any], body: str) -> str:
    """Either the user tapped reject after the handler detached,
    or the approval expired / the background task errored. Either
    way, the reply summarises the outcome — we just surface it
    with a warning glyph so the user can scan it quickly."""
    tool = entry.get("meta", {}).get("tool") or "tool"
    header = f"⚠️ Late rejection from {tool}"
    return _join_non_empty(header, body)


def _format_cron_completed(entry: dict[str, Any], body: str) -> str:
    """A cron firing completed.

    When the cron was silent (``silent: true`` → cron_runner
    drops the body), we return an empty string. The pusher's
    contract is that empty formatted output skips delivery —
    which is the whole point of ``silent``. Returning a "✅
    cron" pingback on every silent firing would defeat the
    flag.

    When there's a body, render the normal "✅ <name>\\n\\n<body>"
    shape. Name resolution: ``meta.cron_name`` (if the runner
    populated it — not today, but reserved) or the event's
    ``title`` (currently "cron '<name>'")."""
    if not body:
        return ""
    name = entry.get("meta", {}).get("cron_name") or entry.get("title", "cron")
    header = f"✅ {name}"
    return _join_non_empty(header, body)


def _format_cron_failed(entry: dict[str, Any], body: str) -> str:
    name = entry.get("meta", {}).get("cron_name") or entry.get("title", "cron")
    header = f"❌ {name} failed"
    return _join_non_empty(header, body)


def _format_agent_message(entry: dict[str, Any], body: str) -> str:
    """``send_message`` tool output. The body is the text the
    model explicitly chose to push. Prefix with the title only if
    it's informative (non-default)."""
    title = entry.get("title", "")
    if title and title != "Agent Message":
        return _join_non_empty(title, body)
    return body or title or "(empty agent message)"


def _format_tool_executed(entry: dict[str, Any], body: str) -> str:
    """Phase 4.7: a ``project_shell`` (or future shell-like
    tool) invocation finished. The user wants to see the
    sequence of commands running — especially for
    ``trust_session`` flows where the approval prompt only
    fired once.

    Glyph depends on outcome:
    - ``▶`` for a successful run (exit 0).
    - ``❌`` for a non-zero exit.
    - ``⏱️`` for a timeout.

    Title is the gateway-formatted ``ran project_shell on
    <project>: <truncated cmd>`` (or ``FAILED``/``TIMED OUT``
    variants); we prefix the glyph and preserve the body which
    carries stdout + stderr. Empty body → ``(no output)``
    marker so the message isn't just a header."""
    meta = entry.get("meta") or {}
    timed_out = bool(meta.get("timed_out"))
    exit_code = meta.get("exit_code")
    title = entry.get("title", "")
    if timed_out:
        glyph = "⏱️"
    elif isinstance(exit_code, int) and exit_code != 0:
        glyph = "❌"
    else:
        glyph = "▶"
    header = f"{glyph} {title}" if title else f"{glyph} tool_executed"
    body_or_marker = body if body else "(no output)"
    return _join_non_empty(header, body_or_marker)


# ---------------------------------------------------- helpers


def _cap(body: Any) -> str:
    s = str(body or "")
    if len(s) <= _BODY_CAP:
        return s
    return s[: _BODY_CAP - len(_TRUNCATED)] + _TRUNCATED


def _join_non_empty(header: str, body: str) -> str:
    """Join a header and a body with a blank line, omitting
    either half if it's empty so we never emit a message that
    starts with a leading blank line."""
    if header and body:
        return f"{header}\n\n{body}"
    return header or body or ""
