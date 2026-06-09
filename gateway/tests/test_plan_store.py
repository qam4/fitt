"""Tests for Phase 12 task 6 — Plan model + PlanStore.

Pure logic. Covers structured round-trip (property C5), markdown
rendering, status helpers, history hydration (Hermes pattern), and
JSON persistence. No model needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from gateway.plan_store import (
    PLAN_STATUSES,
    Plan,
    PlanItem,
    PlanStatus,
    PlanStore,
    derive_plan_from_history,
)


def test_statuses_match_literal() -> None:
    assert set(PLAN_STATUSES) == set(get_args(PlanStatus))


# --------------------------------------------------------------- model


def test_plan_item_round_trip() -> None:
    item = PlanItem(id="1", text="do a thing", status="in_progress")
    assert PlanItem.from_dict(item.to_dict()) == item


def test_plan_item_rejects_bad_status() -> None:
    with pytest.raises(ValueError, match="status"):
        PlanItem.from_dict({"id": "1", "text": "x", "status": "nope"})


def test_plan_item_rejects_missing_id() -> None:
    with pytest.raises(ValueError, match="id"):
        PlanItem.from_dict({"text": "x"})


def test_plan_round_trip_basic() -> None:
    plan = Plan(
        items=[
            PlanItem("1", "search headlines", "done"),
            PlanItem("2", "read top 5", "in_progress"),
            PlanItem("3", "summarise", "pending"),
        ]
    )
    assert Plan.from_dict(plan.to_dict()) == plan


# Property C5: a plan round-trips through JSON without loss.
@settings(max_examples=150)
@given(
    st.lists(
        st.tuples(
            st.text(min_size=1, max_size=12),  # id
            st.text(max_size=40),  # text
            st.sampled_from(PLAN_STATUSES),
        ),
        max_size=8,
    )
)
def test_plan_round_trip_property(rows: list[tuple[str, str, PlanStatus]]) -> None:
    # ids must be unique-ish for a faithful plan; dedupe by index suffix.
    items = [PlanItem(id=f"{i}-{r[0]}", text=r[1], status=r[2]) for i, r in enumerate(rows)]
    plan = Plan(items=items)
    via_json = Plan.from_dict(json.loads(json.dumps(plan.to_dict())))
    assert via_json == plan


def test_render_markdown_shows_statuses() -> None:
    plan = Plan(
        items=[
            PlanItem("1", "alpha", "done"),
            PlanItem("2", "beta", "in_progress"),
            PlanItem("3", "gamma", "pending"),
            PlanItem("4", "delta", "blocked"),
        ]
    )
    md = plan.render_markdown()
    assert "- [x] alpha" in md
    assert "- [~] beta" in md
    assert "- [ ] gamma" in md
    assert "- [!] delta" in md


def test_render_empty_plan() -> None:
    assert Plan().render_markdown() == "(no plan)"


def test_mark_next_pending_is_complete() -> None:
    plan = Plan(items=[PlanItem("1", "a"), PlanItem("2", "b")])
    assert plan.is_complete() is False
    assert plan.next_pending().id == "1"  # type: ignore[union-attr]
    assert plan.mark("1", "done") is True
    assert plan.next_pending().id == "2"  # type: ignore[union-attr]
    assert plan.mark("2", "done") is True
    assert plan.next_pending() is None
    assert plan.is_complete() is True
    assert plan.mark("nope", "done") is False


def test_empty_plan_is_not_complete() -> None:
    assert Plan().is_complete() is False


# --------------------------------------------------------------- history hydration


def _tool_msg(payload: dict[str, Any]) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": "c1", "content": json.dumps(payload)}


def test_derive_plan_from_history_finds_latest() -> None:
    history = [
        {"role": "user", "content": "do stuff"},
        _tool_msg({"todos": [{"id": "1", "text": "old", "status": "done"}]}),
        {"role": "assistant", "content": "working"},
        _tool_msg({"todos": [{"id": "1", "text": "new", "status": "in_progress"}]}),
    ]
    plan = derive_plan_from_history(history)
    assert plan is not None
    assert len(plan.items) == 1
    assert plan.items[0].text == "new"  # the *latest* todo message wins


def test_derive_plan_from_history_none_when_absent() -> None:
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert derive_plan_from_history(history) is None


def test_derive_plan_tolerates_malformed() -> None:
    history = [
        {"role": "tool", "content": '{"todos": not json'},  # malformed
        _tool_msg({"todos": [{"id": "1", "text": "good", "status": "pending"}]}),
        {"role": "tool", "content": '{"todos": [{"id": "", "text": "x"}]}'},  # bad item
    ]
    plan = derive_plan_from_history(history)
    assert plan is not None
    assert plan.items[0].text == "good"


# --------------------------------------------------------------- store


def test_store_in_memory_get_set_clear() -> None:
    store = PlanStore()
    assert store.get("s1") is None
    plan = Plan(items=[PlanItem("1", "a")])
    store.set("s1", plan)
    assert store.get("s1") == plan
    store.clear("s1")
    assert store.get("s1") is None


def test_store_persists_to_disk(tmp_path: Path) -> None:
    path = tmp_path / "plans.json"
    store = PlanStore(path)
    plan = Plan(items=[PlanItem("1", "a", "done"), PlanItem("2", "b", "pending")])
    store.set("sess", plan)
    # A fresh store from the same path reloads the plan losslessly.
    reloaded = PlanStore(path)
    assert reloaded.get("sess") == plan


def test_store_load_tolerates_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "plans.json"
    path.write_text("{ not json", encoding="utf-8")
    store = PlanStore(path)  # must not raise
    assert store.get("anything") is None


def test_hydrate_from_history_only_when_empty() -> None:
    store = PlanStore()
    history = [_tool_msg({"todos": [{"id": "1", "text": "from-history", "status": "pending"}]})]
    got = store.hydrate_from_history("s1", history)
    assert got is not None and got.items[0].text == "from-history"

    # An in-memory plan wins over history (it's more current).
    current = Plan(items=[PlanItem("9", "in-memory", "in_progress")])
    store.set("s2", current)
    got2 = store.hydrate_from_history("s2", history)
    assert got2 == current
