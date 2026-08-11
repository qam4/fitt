"""Seed scenario outcome assertions (Phase D) — crafted trajectories, no live run."""

from __future__ import annotations

from datetime import UTC, datetime

from gateway.e2e_eval import E2ETrajectory, RunResult
from gateway.e2e_scenarios import (
    chitchat_scenario,
    memory_recall_cross_session_scenario,
    memory_recall_scenario,
    news_scenario,
    reminder_scenario,
    seed_scenarios,
    todo_lifecycle_scenario,
    todo_scenario,
)


def _traj(
    *,
    reply: str = "",
    tools: tuple[str, ...] = (),
    snapshot: dict | None = None,
    earlier_tool_calls: tuple[dict, ...] = (),
) -> E2ETrajectory:
    return E2ETrajectory(
        scenario="t",
        turns=[],
        run=RunResult(
            reply=reply,
            tool_sequence=tools,
            earlier_tool_calls=earlier_tool_calls,
        ),
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


def test_same_session_recall_passes_from_history_without_a_tool() -> None:
    """The fact is one turn back, so history carries it. Demanding
    memory_search here punished the cheapest correct answer — and read
    as a model defect when the tool wasn't even registered."""
    scen = memory_recall_scenario(keyword="4821")
    res = scen.outcome_assert(_traj(reply="Your bike lock combination is 4821.", tools=()))
    assert res.passed
    assert "history" in res.reason


def test_same_session_recall_also_passes_via_memory_search() -> None:
    scen = memory_recall_scenario(keyword="4821")
    res = scen.outcome_assert(_traj(reply="It's 4821.", tools=("memory_search:ok",)))
    assert res.passed
    assert "memory_search" in res.reason


def test_same_session_recall_fails_when_the_fact_is_missing() -> None:
    scen = memory_recall_scenario(keyword="4821")
    assert not scen.outcome_assert(_traj(reply="I don't recall.", tools=())).passed


def test_cross_session_recall_requires_memory_search() -> None:
    """Across a session boundary history can't help, so the tool is the
    only path and demanding it is fair."""
    scen = memory_recall_cross_session_scenario(keyword="4821")
    without = scen.outcome_assert(_traj(reply="Your combination is 4821.", tools=()))
    with_tool = scen.outcome_assert(_traj(reply="It's 4821.", tools=("memory_search:ok",)))
    assert not without.passed
    assert not without.inconclusive  # a plain miss is a real failure
    assert with_tool.passed


def test_cross_session_recall_is_inconclusive_when_a_lesson_leaked_the_fact() -> None:
    """The live failure this encodes: the model stored the fact with
    learn_add in session A, lessons go into every system prompt
    regardless of session, so session B answered correctly with no
    retrieval. Scoring that as a model failure had the judge accusing it
    of hallucinating a 1-in-10,000 number."""
    scen = memory_recall_cross_session_scenario(keyword="4821")

    res = scen.outcome_assert(
        _traj(
            reply="Your bike lock combination is 4821.",
            tools=(),
            earlier_tool_calls=({"name": "learn_add", "ok": True},),
        )
    )

    assert res.inconclusive
    assert not res.passed
    assert "learn_add" in res.reason


def test_cross_session_miss_is_a_failure_even_with_an_earlier_lesson() -> None:
    """A lesson only excuses a run that actually produced the fact."""
    scen = memory_recall_cross_session_scenario(keyword="4821")

    res = scen.outcome_assert(
        _traj(
            reply="I have no record of that.",
            tools=(),
            earlier_tool_calls=({"name": "learn_add", "ok": True},),
        )
    )

    assert not res.passed
    assert not res.inconclusive


def test_cross_session_first_turn_avoids_lesson_shaped_phrasing() -> None:
    """ "Note this for later" invites learn_add, which would leave
    retrieval untested on most runs."""
    first = memory_recall_cross_session_scenario().turns[0]

    assert "note this" not in str(first["content"]).lower()


def test_cross_session_scenario_declares_its_prerequisite() -> None:
    scen = memory_recall_cross_session_scenario()
    assert scen.requires_tools == ("memory_search",)
    assert "embedding_alias" in scen.requires_hint


def test_cross_session_turns_run_in_different_sessions() -> None:
    turns = memory_recall_cross_session_scenario().turns
    assert [t["session"] for t in turns] == ["a", "b"]


def test_recall_fact_avoids_fitt_vocabulary() -> None:
    """Regression guard: the original fact used "hub" and "deploy",
    which sent every model into the project registry instead of
    recalling — the scenario measured tool routing, not memory."""
    scen = memory_recall_scenario()
    text = " ".join(str(t["content"]) for t in scen.turns).lower()
    for collision in ("hub", "deploy", "project", "docker"):
        assert collision not in text, f"recall fact reuses FITT vocabulary: {collision!r}"


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
        "memory_recall_cross_session",
        "todo",
        "todo_lifecycle",
    }
    for s in scens:
        assert s.turns and s.rubric  # all judged + non-empty
    # memory_recall and todo_lifecycle are multi-turn.
    assert len(memory_recall_scenario().turns) == 2
    assert len(todo_lifecycle_scenario().turns) == 2
