"""Static schema lint over the real tool registry.

Why this exists
---------------

2026-06-08: ``cron_add`` shipped with three required fields —
``name``, ``message``, ``schedule_spec`` — one of them literally
named ``name``, colliding with the function's own name. A small
model (hermes3:8b) could not set a plain reminder: it supplied
two of the three required fields and oscillated between
"'name' is required" and "'message' is required", never
converging. See docs/observed-issues.md.

The bug was a *static design defect* — visible by inspecting the
schema, no model needed. The existing unit tests (test_tools_cron)
never caught it because they hand-write a *correct* args dict, so
they test the handler, not the schema's fillability. The eval
harness never caught it because (a) it tests synthetic tool
schemas declared inline, not the real registry, and (b) it grades
*which* tool the model calls, not whether the arguments validate.

This module closes the gap at the cheapest, most deterministic
gate: a plain pytest pass over every tool the gateway actually
registers, asserting two ergonomic invariants that don't need a
model to check. It runs free in CI on every commit. A live-model
eval that measures "can model X fill a *well-formed* schema" is a
separate, more expensive question and deliberately not in scope
here.

What it does NOT lint
---------------------

Argument *naming consistency* across tools (e.g. ``cron_add`` uses
``message`` for its prompt, ``send_message`` uses ``text``). That
reads like a lintable invariant but isn't: ``git_commit`` also
uses ``message`` — for a commit message, a genuinely different
concept — so a name-based rule would produce false positives. The
canonical-vocabulary decision plus the breaking rename it implies
is a human design call, tracked in docs/observed-issues.md, not
something a static test can settle.
"""

from __future__ import annotations

from gateway.tools import (
    Tool,
    build_cron_tools,
    build_fileops_tools,
    build_git_tools,
    build_inline_tools,
    build_lessons_tools,
    build_plan_tools,
    build_project_shell_tool,
    build_send_message_tool,
    build_shell_tools,
    build_web_search_tools,
)

# --------------------------------------------------------------- registry


def _all_registered_tools() -> list[Tool]:
    """Every tool the gateway can register, assembled from the
    real builders.

    We pass minimal stand-ins where a builder needs an argument:
    ``build_inline_tools`` only uses its ``registry_ref`` inside
    the ``list_capabilities`` closure (never at schema-build
    time), and ``build_web_search_tools`` only needs a backend
    name string. Neither affects the schemas we lint, and we
    never invoke the callables here.
    """
    tools: list[Tool] = []
    tools += build_cron_tools()
    tools += build_fileops_tools()
    tools += build_git_tools()
    tools += build_inline_tools(registry_ref=None)
    tools += build_lessons_tools()
    tools += build_plan_tools()
    tools.append(build_project_shell_tool())
    tools.append(build_send_message_tool())
    tools += build_shell_tools()
    tools += build_web_search_tools(backend_name="duckduckgo")
    return tools


def _required(tool: Tool) -> list[str]:
    req = tool.schema.get("required", [])
    assert isinstance(req, list), f"{tool.name}: 'required' must be a list"
    return [str(r) for r in req]


# --------------------------------------------------------------- sanity


def test_registry_assembles_and_names_are_unique() -> None:
    """Guard against a builder regressing to a non-list schema or
    two tools shipping the same name (which would make the
    capability block ambiguous)."""
    tools = _all_registered_tools()
    assert tools, "no tools assembled — a builder import probably broke"
    names = [t.name for t in tools]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"duplicate tool names in the registry: {sorted(dupes)}"


# --------------------------------------------------------------- collision


def test_no_required_field_collides_with_tool_name() -> None:
    """A required field named after the tool itself is a fumble
    magnet: the model sees ``cron_add`` wants a required ``name``
    and conflates the two. This is the exact 2026-06-08 cron bug.
    Zero legitimate tool needs a required field named after
    itself, so this is a hard rule with no exceptions."""
    offenders = []
    for tool in _all_registered_tools():
        if tool.name in _required(tool):
            offenders.append(tool.name)
    assert not offenders, (
        "these tools have a required field named after the tool itself "
        f"(the cron_add trap): {offenders}. Rename the field or make it "
        "optional and derive it."
    )


# --------------------------------------------------------------- budget

_REQUIRED_FIELD_BUDGET = 2
"""A model has to fill every required field correctly in one shot;
each additional required field multiplies the fumble surface. Two
is the ceiling a tool may have without explicit review. Tools that
genuinely need more must be listed in ``_BUDGET_EXCEPTIONS`` with a
reason, which forces the decision to be conscious rather than an
accretion no one noticed."""

_BUDGET_EXCEPTIONS: dict[str, str] = {
    "write_file": "project + path + content are all load-bearing for a write",
    "edit_file": "project + path + old_str + new_str are all load-bearing for a surgical edit",
    "spec_mark_task": "project + feature + task_id are the minimal address of one checkbox",
}


def test_required_field_budget_respected() -> None:
    """No tool exceeds the required-field budget unless it's an
    explicitly reviewed exception. The original ``cron_add`` (3
    required) would have tripped this; the fixed one (2) passes."""
    over_budget = {
        tool.name: _required(tool)
        for tool in _all_registered_tools()
        if len(_required(tool)) > _REQUIRED_FIELD_BUDGET
    }
    unreviewed = {n: r for n, r in over_budget.items() if n not in _BUDGET_EXCEPTIONS}
    assert not unreviewed, (
        f"these tools exceed the {_REQUIRED_FIELD_BUDGET}-required-field budget "
        f"without review: {unreviewed}. Either reduce required fields (make some "
        "optional and derive/default them) or add an entry to _BUDGET_EXCEPTIONS "
        "with a one-line justification."
    )


def test_budget_exceptions_are_not_stale() -> None:
    """Every allowlisted exception must still exist and still
    exceed the budget. Stops the allowlist from carrying dead
    entries that would silently permit a future regression."""
    by_name = {t.name: t for t in _all_registered_tools()}
    for name in _BUDGET_EXCEPTIONS:
        assert name in by_name, f"_BUDGET_EXCEPTIONS lists unknown tool {name!r}"
        n = len(_required(by_name[name]))
        assert n > _REQUIRED_FIELD_BUDGET, (
            f"{name!r} is in _BUDGET_EXCEPTIONS but only has {n} required "
            f"field(s) now — at or under budget. Remove the stale exception."
        )
