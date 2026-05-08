"""Phase 5 — ``learn_*`` inline tools.

Three tools mirror the shape of Phase 4.5's ``cron_*`` group:

* ``learn_add(text, category?)`` — append a lesson. Default
  bucket ``ask`` (adding a persistent correction deserves a
  human confirmation).
* ``learn_list()`` — read current lessons. Default bucket
  ``auto`` (inspecting is low-risk).
* ``learn_remove(substring)`` — remove matches. Default bucket
  ``ask`` (removing a lesson changes behaviour persistently).

Each tool looks up :class:`~gateway.lessons.LessonsStore` off
the :class:`ToolContext`. Tests supply a ``LessonsStore``
directly; at runtime the gateway wires it in ``create_app``.
"""

from __future__ import annotations

import logging
from typing import Any

from ._types import ApprovalBucket, Tool, ToolContext, ToolResult

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- schemas


_SCHEMA_LEARN_ADD: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": (
                "The correction or preference to remember. "
                "One or two short sentences work best; long "
                "prose stops being useful at scale. Will be "
                "injected verbatim into every future system "
                "prompt as a bullet under `[Learned "
                "corrections]`."
            ),
        },
        "category": {
            "type": "string",
            "description": (
                "Optional short tag for grouping (e.g. "
                "'tooling', 'style', 'preferences'). Appears "
                "in square brackets at the start of the "
                "rendered bullet."
            ),
            "default": "",
        },
    },
    "required": ["text"],
    "additionalProperties": False,
}


_SCHEMA_LEARN_LIST: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


_SCHEMA_LEARN_REMOVE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "substring": {
            "type": "string",
            "description": (
                "Case-insensitive substring to match against "
                "each lesson's text. Every lesson whose text "
                "contains this substring is removed. Empty / "
                "whitespace-only values are rejected to "
                "prevent accidental wipes."
            ),
        },
    },
    "required": ["substring"],
    "additionalProperties": False,
}


# --------------------------------------------------------------- implementation


def _get_lessons_store(ctx: ToolContext) -> Any | None:
    store = getattr(ctx, "lessons", None)
    return store


async def _tool_learn_add(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    store = _get_lessons_store(ctx)
    if store is None:
        return ToolResult.error(
            "lessons store not available on this gateway (bug: "
            "the store should be wired in create_app)"
        )

    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        return ToolResult.error("'text' is required and must be non-empty")

    category = args.get("category") or ""
    if not isinstance(category, str):
        return ToolResult.error("'category' must be a string if provided")

    try:
        lesson = store.add(text, category=category or None)
    except ValueError as e:
        return ToolResult.error(str(e))

    return ToolResult.ok(
        f"learned: {lesson.render()[2:]}"  # strip "- " prefix for a cleaner message
    )


async def _tool_learn_list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    _ = args
    store = _get_lessons_store(ctx)
    if store is None:
        return ToolResult.error("lessons store not available on this gateway (bug)")
    lessons = store.read()
    if not lessons:
        return ToolResult.ok("no lessons recorded yet")
    lines = [lsn.render() for lsn in lessons]
    return ToolResult.ok("\n".join(lines))


async def _tool_learn_remove(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    store = _get_lessons_store(ctx)
    if store is None:
        return ToolResult.error("lessons store not available on this gateway (bug)")

    substring = args.get("substring")
    if not isinstance(substring, str) or not substring.strip():
        return ToolResult.error(
            "'substring' is required and must be non-empty. "
            "Removing with an empty substring would wipe every "
            "lesson; refuse rather than silently erase."
        )

    removed = store.remove(substring)
    if removed == 0:
        return ToolResult.ok(f"no lessons matched {substring!r}")
    return ToolResult.ok(f"removed {removed} lesson(s) matching {substring!r}")


# --------------------------------------------------------------- factory


def build_lessons_tools() -> list[Tool]:
    """Return the three ``learn_*`` tools ready for registration."""
    return [
        Tool(
            name="learn_add",
            description=(
                "Record a correction or preference that should "
                "persist across sessions. The user saying "
                "'remember X', 'always do Y', or 'never Z' is a "
                "signal to call this. Injected as a bullet "
                "under `[Learned corrections]` in every future "
                "system prompt. Keep each entry short."
            ),
            schema=_SCHEMA_LEARN_ADD,
            callable=_tool_learn_add,
            default_bucket=ApprovalBucket.ASK,
            requires_project=False,
        ),
        Tool(
            name="learn_list",
            description=(
                "Show the current learned corrections. Useful "
                "when the user asks 'what have you learned' or "
                "when you want to check whether a preference is "
                "already recorded before adding a new one."
            ),
            schema=_SCHEMA_LEARN_LIST,
            callable=_tool_learn_list,
            default_bucket=ApprovalBucket.AUTO,
            requires_project=False,
        ),
        Tool(
            name="learn_remove",
            description=(
                "Remove lessons whose text contains `substring` "
                "(case-insensitive). The user saying 'forget X' "
                "or 'you no longer need to Y' is a signal to "
                "call this. Empty substrings are rejected to "
                "prevent accidental wipes."
            ),
            schema=_SCHEMA_LEARN_REMOVE,
            callable=_tool_learn_remove,
            default_bucket=ApprovalBucket.ASK,
            requires_project=False,
        ),
    ]
