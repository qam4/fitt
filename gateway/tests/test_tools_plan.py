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


async def test_todowrite_rejects_bad_status() -> None:
    result = await _tool().callable({"todos": [{"text": "x", "status": "wat"}]}, _ctx(PlanStore()))
    assert result.is_error


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
