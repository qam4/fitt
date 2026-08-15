"""Tests for Phase 12 task 7 — the todowrite plan tool.

Tool-layer behavior: writes the plan to the PlanStore, returns the
todos payload (for history hydration), normalizes ids, defaults
status, validates input, and fails readably when the store isn't
wired.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.plan_store import PlanStore
from gateway.projects import ProjectRegistry
from gateway.tools import Tool, ToolContext, build_plan_tools


def _tool() -> Tool:
    tools = {t.name: t for t in build_plan_tools()}
    return tools["todowrite"]


def _ctx(store: PlanStore | None) -> ToolContext:
    return ToolContext(
        client="telegram",
        session_key="main",
        projects=ProjectRegistry(Path("nonexistent.yaml")),
        plan_store=store,
    )


async def test_todowrite_writes_to_store_and_returns_payload() -> None:
    store = PlanStore()
    result = await _tool().callable(
        {"todos": [{"text": "search news"}, {"text": "summarise", "status": "pending"}]},
        _ctx(store),
    )
    assert not result.is_error
    # Result content is the {"todos": [...]} payload (history hydration).
    payload = json.loads(result.payload)
    assert [t["text"] for t in payload["todos"]] == ["search news", "summarise"]
    # Persisted to the store for the session.
    plan = store.get("main")
    assert plan is not None
    assert len(plan.items) == 2
    assert plan.items[0].id == "1"  # auto-numbered
    assert plan.items[0].status == "pending"  # defaulted


async def test_todowrite_preserves_supplied_ids_and_status() -> None:
    store = PlanStore()
    await _tool().callable(
        {"todos": [{"id": "a", "text": "x", "status": "done"}]},
        _ctx(store),
    )
    plan = store.get("main")
    assert plan is not None
    assert plan.items[0].id == "a"
    assert plan.items[0].status == "done"


async def test_todowrite_replaces_previous_plan() -> None:
    store = PlanStore()
    tool = _tool()
    await tool.callable({"todos": [{"text": "first"}]}, _ctx(store))
    await tool.callable({"todos": [{"text": "second"}, {"text": "third"}]}, _ctx(store))
    plan = store.get("main")
    assert plan is not None
    assert [i.text for i in plan.items] == ["second", "third"]


async def test_todowrite_requires_todos_list() -> None:
    result = await _tool().callable({"todos": "nope"}, _ctx(PlanStore()))
    assert result.is_error
    assert "todos" in result.payload


async def test_todowrite_rejects_empty_text() -> None:
    result = await _tool().callable({"todos": [{"text": "  "}]}, _ctx(PlanStore()))
    assert result.is_error
    assert "text" in result.payload


async def test_todowrite_keeps_the_plan_when_the_status_is_junk() -> None:
    """Deliberate contract change, 2026-08-14. This test previously asserted
    an unknown status was an ERROR.

    Rejecting the whole write threw away a usable plan over a metadata
    field: the step text is the payload, and `todowrite` is the planner's
    only output channel, so a rejected call can cost the entire turn (a live
    hermes3 run produced an empty reply that way). An unrecognised status now
    normalises to `pending`. Recorded here rather than silently deleted,
    because a test flipping direction should be visible in review."""
    result = await _tool().callable({"todos": [{"text": "x", "status": "wat"}]}, _ctx(PlanStore()))

    assert not result.is_error
    assert '"status": "pending"' in result.payload


async def test_todowrite_fails_readably_without_store() -> None:
    result = await _tool().callable({"todos": [{"text": "x"}]}, _ctx(None))
    assert result.is_error
    assert "plan store not available" in result.payload


def test_todowrite_is_auto_bucket() -> None:
    from gateway.tools import ApprovalBucket

    assert _tool().default_bucket is ApprovalBucket.AUTO


@pytest.mark.parametrize("missing", ["text"])
def test_todowrite_schema_requires_text_per_item(missing: str) -> None:
    schema = _tool().schema
    item_schema = schema["properties"]["todos"]["items"]
    assert missing in item_schema["required"]
    assert item_schema["additionalProperties"] is False


# ------------------------------------------- fumble surface
#
# Measured 2026-08-14 on a live hermes3:8b planned run: it elected to plan
# in 9 of 15 scenarios and sent `{"todos": ["step one", "step two"]}` every
# time, getting back "todos[0] must be an object with a 'text' field". On
# one scenario the failed plan call was the turn's ONLY tool call and the
# user got an empty reply. todowrite is the planner's sole output channel,
# so a fumble here costs the whole turn — the exact "schema fumble-trap"
# class the Phase 12 requirements cite as the phase's reason for existing.


async def test_a_plain_list_of_strings_is_accepted() -> None:
    """The shape models actually emit for a task list."""
    store = PlanStore()

    res = await _tool().callable({"todos": ["fetch the news", "summarise it"]}, _ctx(store))

    assert not res.is_error
    plan = store.get("main")
    assert plan is not None
    assert [i.text for i in plan.items] == ["fetch the news", "summarise it"]
    assert [i.status for i in plan.items] == ["pending", "pending"]


async def test_strings_and_objects_can_be_mixed() -> None:
    store = PlanStore()

    res = await _tool().callable(
        {"todos": ["read the list", {"text": "act on it", "status": "done"}]}, _ctx(store)
    )

    assert not res.is_error
    plan = store.get("main")
    assert plan is not None
    assert [(i.text, i.status) for i in plan.items] == [
        ("read the list", "pending"),
        ("act on it", "done"),
    ]


async def test_a_near_miss_status_is_normalised_not_rejected() -> None:
    """The status is metadata; the step text is the payload. Failing a whole
    plan because a model wrote "completed" instead of "done" trades a usable
    plan for a pedantic error."""
    store = PlanStore()

    res = await _tool().callable(
        {
            "todos": [
                {"text": "a", "status": "completed"},
                {"text": "b", "status": "in-progress"},
                {"text": "c", "status": "not_started"},
                {"text": "d", "status": "banana"},
            ]
        },
        _ctx(store),
    )

    assert not res.is_error
    plan = store.get("main")
    assert plan is not None
    assert [i.status for i in plan.items] == ["done", "in_progress", "pending", "pending"]


async def test_a_non_string_non_object_step_still_errors_readably() -> None:
    """Coercion has a limit, and the message must show the accepted shape."""
    res = await _tool().callable({"todos": [42]}, _ctx(PlanStore()))

    assert res.is_error
    assert "plain string" in res.payload
    assert '"text"' in res.payload
