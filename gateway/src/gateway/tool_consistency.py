"""Boot-time lint of registered tool schemas (BACKLOG: tool ergonomics).

The eval suites measure the *model* (can it tool-call?) with a
representative handful of cases. This module is the other half of that
split (see project-overview steering, "measurement ladder"): a cheap,
**model-independent** check of the *tools themselves* — do the shipped
schemas have the ergonomics footguns that make even a capable model
fumble?

It reads whatever's registered (``registry.list_all()``), so it covers
inline, MCP, and skill-provided tools uniformly — the thing a
hand-written per-tool eval case never could, because MCP/skills tools
aren't known when the cases are written.

Pure and synchronous: takes a sequence of :class:`Tool` and returns a
list of human-readable warning strings, exactly the shape of
:func:`gateway.config.check_missing_api_keys`. The caller logs each at
ERROR at boot and the dashboard Settings page re-runs it into the
"Boot-time warnings" card — surfaced, not just logged.

v1 flags three footguns; the checks are a menu, extend by appending:

1. **Inconsistent payload-field naming.** When tools disagree on the
   name of the same conceptual field, the model's prior from one tool
   leaks into another and it types the wrong arg name — the live
   ``cron_add`` (``message``) vs ``send_message`` / ``learn_add``
   (``text``) fumble.
2. **Heavy required-arg surface.** A tool with many required fields is
   more places to slip (the ``edit_file`` shape).
3. **Missing tool description.** The model leans on it to pick a tool.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .tools import Tool


# Field-name synonym groups: names that tend to mean the same thing.
# When tools in the registry use more than one variant from a group,
# the model fumbles the arg name (the cron_add/send_message bug). Kept
# deliberately narrow and evidence-based to avoid false positives — a
# group earns its place from an observed fumble, not a guess. Append a
# group when a new class of inconsistency bites.
_SYNONYM_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"text", "message", "msg", "body", "content"}),
)


# A tool asking for more than this many required args is a fumble risk.
# Not wrong — some tools genuinely need the fields (edit_file's exact
# replace) — but worth surfacing so the schema gets a second look.
_MAX_COMFORTABLE_REQUIRED = 3


def _properties(tool: Tool) -> dict[str, Any]:
    props = tool.schema.get("properties")
    return props if isinstance(props, dict) else {}


def _required(tool: Tool) -> list[str]:
    req = tool.schema.get("required")
    return [r for r in req if isinstance(r, str)] if isinstance(req, list) else []


def _check_payload_naming(tools: Sequence[Tool]) -> list[str]:
    """Flag synonym groups where >1 variant is used across the registry."""
    warnings: list[str] = []
    for group in _SYNONYM_GROUPS:
        # variant name -> tools that use it.
        users: dict[str, list[str]] = {}
        for tool in tools:
            for field in _properties(tool):
                if field in group:
                    users.setdefault(field, []).append(tool.name)
        if len(users) > 1:
            parts = ", ".join(
                f"`{variant}` ({', '.join(sorted(names))})"
                for variant, names in sorted(users.items())
            )
            warnings.append(
                f"tools disagree on the name of the same conceptual field: {parts}. "
                f"The model's prior from one tool leaks into another and it types the "
                f"wrong arg name (the cron_add/send_message fumble). Pick one name and "
                f"align the schemas."
            )
    return warnings


def _check_required_surface(tools: Sequence[Tool]) -> list[str]:
    warnings: list[str] = []
    for tool in sorted(tools, key=lambda t: t.name):
        required = _required(tool)
        if len(required) > _MAX_COMFORTABLE_REQUIRED:
            warnings.append(
                f"tool {tool.name!r} requires {len(required)} fields "
                f"({', '.join(required)}) — many required args raise the fumble rate. "
                f"Consider optional fields with sensible defaults, or splitting the tool."
            )
    return warnings


def _check_descriptions(tools: Sequence[Tool]) -> list[str]:
    warnings: list[str] = []
    for tool in sorted(tools, key=lambda t: t.name):
        if not tool.description.strip():
            warnings.append(
                f"tool {tool.name!r} has no description — the model leans on it to pick "
                f"a tool; an empty one raises wrong-tool and narration rates."
            )
    return warnings


def check_tool_consistency(tools: Sequence[Tool]) -> list[str]:
    """Return human-readable warnings for tool-schema footguns.

    Pure: reads the tools' names/descriptions/schemas only. Empty list
    when everything's clean. The caller logs each at ERROR (boot) and the
    Settings page renders them; nothing here mutates or blocks."""
    warnings: list[str] = []
    warnings.extend(_check_payload_naming(tools))
    warnings.extend(_check_required_surface(tools))
    warnings.extend(_check_descriptions(tools))
    return warnings


__all__ = ["check_tool_consistency"]
