"""Seed scenario outcome assertions (Phase D) — crafted trajectories, no live run."""

from __future__ import annotations

from datetime import UTC, datetime

from gateway.e2e_eval import E2ETrajectory, RunResult
from gateway.e2e_scenarios import (
    chitchat_scenario,
    memory_recall_scenario,
    news_scenario,
    reminder_scenario,
    seed_scenarios,
    todo_lifecycle_scenario,
    todo_scenario,
)


def _traj(
    *, reply: str = "", tools: tuple[str, ...] = (), snapshot: dict | None = None
) -> E2ETrajectory:
    return E2ETrajectory(
        scenario="t",
        turns=[],
        run=RunResult(reply=reply, tool_sequence=tools),
        snapshot=snapshot or {},
    )


# --------------------------------------------------------------- reminder


def test_reminder_passes_on_future_at_cron() -> None:
    scen = reminder_scenario(subject="doctor")
    at = datetime.now(UTC).timestamp() + 20 * 3600  # ~20h out
    snap = {"cron_jobs": [{"schedule_kind": "at", "at_ts": at, "message": "call the doctor"}]}
    assert scen.outcome_assert(_traj(snapshot=snap)).passed


def test_reminder_fails_without_cron() -> None:
    assert not reminder_scenario().outcome_assert(_traj(snapshot={"cron_jobs": []})).passed


def test_reminder_fails_on_past_or_wrong_subject() -> None:
    scen = reminder_scenario(subject="doctor")
    past = datetime.now(UTC).timestamp() - 3600
    future_wrong = datetime.now(UTC).timestamp() + 20 * 3600
    snap = {
        "cron_jobs": [
            {"schedule_kind": "at", "at_ts": past, "message": "call the doctor"},
            {"schedule_kind": "at", "at_ts": future_wrong, "message": "buy milk"},
        ]
    }
    assert not scen.outcome_assert(_traj(snapshot=snap)).passed


# --------------------------------------------------------------- news


def test_news_passes_with_search_and_substantive_reply() -> None:
    scen = news_scenario(topic="technology")
    res = scen.outcome_assert(_traj(reply="x" * 120, tools=("web_search:ok",)))
    assert res.passed


def test_news_fails_without_search() -> None:
    scen = news_scenario()
    assert not scen.outcome_assert(_traj(reply="x" * 120, tools=())).passed


def test_news_fails_on_short_reply() -> None:
    scen = news_scenario()
    assert not scen.outcome_assert(_traj(reply="nope", tools=("web_search:ok",))).passed


# --------------------------------------------------------------- memory


def test_memory_passes_when_search_fired_and_fact_recalled() -> None:
    scen = memory_recall_scenario(keyword="docker compose")
    res = scen.outcome_assert(
        _traj(reply="You deploy with docker compose on the hub.", tools=("memory_search",))
    )
    assert res.passed


def test_memory_fails_if_search_not_called() -> None:
    scen = memory_recall_scenario(keyword="docker compose")
    assert not scen.outcome_assert(_traj(reply="docker compose", tools=())).passed


def test_memory_fails_if_fact_not_recalled() -> None:
    scen = memory_recall_scenario(keyword="docker compose")
    assert not scen.outcome_assert(_traj(reply="I don't recall.", tools=("memory_search",))).passed


# --------------------------------------------------------------- todo


def test_todo_passes_when_item_in_todos_text() -> None:
    scen = todo_scenario(item="call the doctor")
    snap = {"todos_text": "# Todos\n## Open\n- [ ] call the doctor\n"}
    assert scen.outcome_assert(_traj(snapshot=snap)).passed


def test_todo_fails_when_item_absent() -> None:
    scen = todo_scenario(item="call the doctor")
    assert not scen.outcome_assert(_traj(snapshot={"todos_text": "- [ ] buy milk"})).passed


def test_todo_fails_with_empty_snapshot() -> None:
    assert not todo_scenario().outcome_assert(_traj(snapshot={})).passed


# --------------------------------------------------------------- chitchat


def test_chitchat_passes_on_reply_with_no_tool() -> None:
    scen = chitchat_scenario()
    assert scen.outcome_assert(_traj(reply="Doing well, thanks for asking!")).passed


def test_chitchat_fails_on_empty_reply() -> None:
    assert not chitchat_scenario().outcome_assert(_traj(reply="   ")).passed


def test_chitchat_fails_when_a_tool_fired() -> None:
    scen = chitchat_scenario()
    res = scen.outcome_assert(_traj(reply="hi", tools=("web_search:ok",)))
    assert not res.passed


# --------------------------------------------------------------- todo lifecycle


def test_todo_lifecycle_passes_when_item_done() -> None:
    scen = todo_lifecycle_scenario(item="buy milk")
    snap = {"todos_text": "## Open\n- [x] buy milk\n"}
    assert scen.outcome_assert(_traj(snapshot=snap)).passed


def test_todo_lifecycle_fails_when_present_but_open() -> None:
    scen = todo_lifecycle_scenario(item="buy milk")
    snap = {"todos_text": "## Open\n- [ ] buy milk\n"}
    res = scen.outcome_assert(_traj(snapshot=snap))
    assert not res.passed
    assert "not marked done" in res.reason


def test_todo_lifecycle_fails_when_absent() -> None:
    scen = todo_lifecycle_scenario(item="buy milk")
    assert not scen.outcome_assert(_traj(snapshot={"todos_text": "- [x] walk dog"})).passed


# --------------------------------------------------------------- set


def test_seed_scenarios_have_rubrics_and_turns() -> None:
    scens = seed_scenarios()
    assert {s.name for s in scens} == {
        "chitchat",
        "reminder",
        "news_summary",
        "memory_recall",
        "todo",
        "todo_lifecycle",
    }
    for s in scens:
        assert s.turns and s.rubric  # all judged + non-empty
    # memory_recall and todo_lifecycle are multi-turn.
    assert len(memory_recall_scenario().turns) == 2
    assert len(todo_lifecycle_scenario().turns) == 2
