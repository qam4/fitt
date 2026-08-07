"""Seed scenario outcome assertions (Phase D) — crafted trajectories, no live run."""

from __future__ import annotations

from datetime import UTC, datetime

from gateway.e2e_eval import E2ETrajectory, RunResult
from gateway.e2e_scenarios import (
    memory_recall_scenario,
    news_scenario,
    reminder_scenario,
    seed_scenarios,
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


# --------------------------------------------------------------- set


def test_seed_scenarios_have_rubrics_and_turns() -> None:
    scens = seed_scenarios()
    assert {s.name for s in scens} == {"reminder", "news_summary", "memory_recall"}
    for s in scens:
        assert s.turns and s.rubric  # all judged + non-empty
    # memory_recall is multi-turn.
    assert len(memory_recall_scenario().turns) == 2
