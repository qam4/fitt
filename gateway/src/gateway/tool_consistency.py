"""Boot-time lint of registered tool schemas (BACKLOG: tool ergonomics).

The eval suites measure the *model* (can it tool-call?) with a
representative handful of cases. This is the other half of that split
(see project-overview steering, "measurement ladder"): a cheap,
**model-independent** check of the *tools themselves*.

It reads whatever's registered (``registry.list_all()``), so it also
covers MCP- and skill-provided tools — which the static CI lint in
``test_tool_schema_lint.py`` can't, because those register at runtime,
after test collection. That runtime/MCP coverage (surfaced on the
dashboard Settings "Boot-time warnings" card) is this module's reason to
exist; the CI test remains the authoritative hard gate for inline tools.

Pure and synchronous: takes a sequence of :class:`Tool` and returns a
list of human-readable warning strings, exactly the shape of
:func:`gateway.config.check_missing_api_keys`. The caller logs each at
ERROR at boot and the Settings page re-runs it — surfaced, not just
logged.

Two footguns, both false-positive-free:

1. **Text-payload family drift.** A small, explicit family of tools takes
   a free-text payload that plays the same role (a message to send, a
   reminder prompt to fire, a note to learn); they should share one arg
   name so the model's prior from one doesn't make it fumble another
   (the live ``cron_add`` `message` vs ``send_message`` `text` fumble).
   We key on the **explicit family + a canonical name**, NOT a blanket
   synonym scan: ``git_commit`` also has a ``message`` field, but that's
   a *commit* message — a genuinely different concept — and a name-only
   rule can't tell the two apart (the exact false positive
   ``test_tool_schema_lint`` documents). The family excludes it by design.
2. **Missing tool description.** The model leans on it to pick a tool.

Deliberately NOT re-implemented here (owned by ``test_tool_schema_lint``,
which handles reviewed exceptions this flat pass can't): the
required-field budget and the field-named-after-the-tool collision rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .tools import Tool


# The "free text to deliver / act on" tool family: a message to send, a
# reminder prompt to fire, a note to learn. Same role for the model, so
# they share one arg name. Keyed explicitly (not a synonym scan) so
# git_commit's `message` (a commit message, a different concept) is never
# swept in. Add a tool here when it joins the family.
_TEXT_PAYLOAD_FAMILY = frozenset({"send_message", "learn_add", "cron_add", "cron_update"})

# The canonical arg name the family should use, and the off-canonical
# names that count as "drifted" if a family member uses one instead.
_TEXT_PAYLOAD_CANONICAL = "text"
_TEXT_PAYLOAD_ALIASES = frozenset({"message", "msg", "body", "content"})


def _properties(tool: Tool) -> dict[str, Any]:
    props = tool.schema.get("properties")
    return props if isinstance(props, dict) else {}


def _check_payload_naming(tools: Sequence[Tool]) -> list[str]:
    """Flag a text-payload-family tool that uses an off-canonical arg."""
    warnings: list[str] = []
    for tool in sorted(tools, key=lambda t: t.name):
        if tool.name not in _TEXT_PAYLOAD_FAMILY:
            continue
        props = _properties(tool)
        if _TEXT_PAYLOAD_CANONICAL in props:
            continue  # already canonical
        drifted = sorted(f for f in props if f in _TEXT_PAYLOAD_ALIASES)
        if drifted:
            warnings.append(
                f"tool {tool.name!r} names its free-text payload {drifted[0]!r}, but "
                f"the text-payload family (send_message / learn_add / cron_*) uses "
                f"{_TEXT_PAYLOAD_CANONICAL!r}. Align it so the model doesn't fumble the "
                f"arg name across tools (the cron_add/send_message fumble)."
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
    warnings.extend(_check_descriptions(tools))
    return warnings


__all__ = ["check_tool_consistency"]
