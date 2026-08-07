"""Phase E — ``todo_*`` inline tools (untimed task list).

Mirrors the ``learn_*`` group: validate args, look up the
:class:`~gateway.todos.TodoStore` off the :class:`ToolContext`,
delegate, return a string payload. Buckets: add / list / done are
``auto`` (low-risk personal list edits); ``todo_remove`` is ``ask``
(deletion is the only lossy op). ``text`` matches the send_message /
learn_add / cron text-payload family.
"""

from __future__ import annotations

from typing import Any

from ._types import ApprovalBucket, Tool, ToolContext, ToolResult

_SCHEMA_ADD: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": (
                "The task to add to the todo list (e.g. 'call the doctor'). "
                "For time-based reminders use cron_add instead."
            ),
        }
    },
    "required": ["text"],
    "additionalProperties": False,
}

_SCHEMA_LIST: dict[str, Any] = {
    "type": "object",
    "properties": {
        "include_done": {
            "type": "boolean",
            "description": "Also show completed items.",
            "default": False,
        }
    },
    "additionalProperties": False,
}

_SCHEMA_SUBSTRING: dict[str, Any] = {
    "type": "object",
    "properties": {
        "substring": {
            "type": "string",
            "description": "Case-insensitive substring identifying the item(s).",
        }
    },
    "required": ["substring"],
    "additionalProperties": False,
}


def _store(ctx: ToolContext) -> Any | None:
    return getattr(ctx, "todos", None)


async def _tool_todo_add(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    store = _store(ctx)
    if store is None:
        return ToolResult.error("todo store not available on this gateway")
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        return ToolResult.error("'text' is required and must be non-empty")
    todo = store.add(text)
    return ToolResult.ok(f"added todo: {todo.text}")


async def _tool_todo_list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    store = _store(ctx)
    if store is None:
        return ToolResult.error("todo store not available on this gateway")
    include_done = bool(args.get("include_done", False))
    todos = store.read() if include_done else store.open_todos()
    if not todos:
        return ToolResult.ok("no todos" if include_done else "no open todos")
    return ToolResult.ok("\n".join(t.render() for t in todos))


async def _tool_todo_done(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    store = _store(ctx)
    if store is None:
        return ToolResult.error("todo store not available on this gateway")
    substring = args.get("substring")
    if not isinstance(substring, str) or not substring.strip():
        return ToolResult.error("'substring' is required and must be non-empty")
    n = store.mark_done(substring)
    return ToolResult.ok(f"marked {n} todo(s) done" if n else f"no open todo matched {substring!r}")


async def _tool_todo_remove(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    store = _store(ctx)
    if store is None:
        return ToolResult.error("todo store not available on this gateway")
    substring = args.get("substring")
    if not isinstance(substring, str) or not substring.strip():
        return ToolResult.error(
            "'substring' is required and must be non-empty (an empty value would wipe the list)"
        )
    n = store.remove(substring)
    return ToolResult.ok(f"removed {n} todo(s)" if n else f"no todo matched {substring!r}")


def build_todo_tools() -> list[Tool]:
    """Return the four ``todo_*`` tools ready for registration."""
    return [
        Tool(
            name="todo_add",
            description=(
                "Add an untimed task to the user's todo list. The user saying "
                "'add X to my todos', 'I need to Y', or 'remind me to Z' (with no "
                "specific time) is a signal to call this. For a time-based reminder, "
                "use cron_add instead."
            ),
            schema=_SCHEMA_ADD,
            callable=_tool_todo_add,
            default_bucket=ApprovalBucket.AUTO,
            requires_project=False,
        ),
        Tool(
            name="todo_list",
            description=(
                "Show the user's open todos (set include_done to also show completed "
                "items). Call this when the user asks what's on their list."
            ),
            schema=_SCHEMA_LIST,
            callable=_tool_todo_list,
            default_bucket=ApprovalBucket.AUTO,
            requires_project=False,
        ),
        Tool(
            name="todo_done",
            description=(
                "Mark matching open todo(s) complete. The user saying 'I did X' or "
                "'mark X done' is a signal to call this."
            ),
            schema=_SCHEMA_SUBSTRING,
            callable=_tool_todo_done,
            default_bucket=ApprovalBucket.AUTO,
            requires_project=False,
        ),
        Tool(
            name="todo_remove",
            description=(
                "Delete matching todo(s) from the list entirely (use todo_done to "
                "just complete them). Empty substrings are rejected."
            ),
            schema=_SCHEMA_SUBSTRING,
            callable=_tool_todo_remove,
            default_bucket=ApprovalBucket.ASK,
            requires_project=False,
        ),
    ]
