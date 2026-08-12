"""Seed scenario outcome assertions (Phase D) — crafted trajectories, no live run."""

from __future__ import annotations

from datetime import UTC, datetime

from gateway.e2e_eval import E2ETrajectory, RunResult
from gateway.e2e_scenarios import (
    chitchat_scenario,
    cron_fires_scenario,
    memory_recall_cross_session_scenario,
    memory_recall_scenario,
    news_scenario,
    notify_scenario,
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


def test_cross_session_recall_is_inconclusive_when_a_lesson_holds_the_fact() -> None:
    """Twice-observed live failure: the fact ends up in the global
    [Learned corrections] block (once via learn_add in the same
    scenario, once leaked from a *different* scenario in the same run),
    so the recall turn answers with no retrieval. Scored as a failure,
    the judge accused a correct model of hallucinating a 4-digit
    number."""
    scen = memory_recall_cross_session_scenario(keyword="7391")

    res = scen.outcome_assert(
        _traj(
            reply="Your gym locker number is 7391.",
            tools=(),
            snapshot={"lessons_text": "[Learned corrections]\n- My gym locker number is 7391."},
        )
    )

    assert res.inconclusive
    assert not res.passed
    assert "Learned corrections" in res.reason


def test_cross_session_miss_is_a_failure_even_when_a_lesson_holds_the_fact() -> None:
    """A lesson only excuses a run that actually produced the fact."""
    scen = memory_recall_cross_session_scenario(keyword="7391")

    res = scen.outcome_assert(
        _traj(
            reply="I have no record of that.",
            tools=(),
            snapshot={"lessons_text": "- My gym locker number is 7391."},
        )
    )

    assert not res.passed
    assert not res.inconclusive


def test_the_two_recall_scenarios_use_different_facts() -> None:
    """They share a run home, and the same-session scenario stores its
    fact as a lesson — a shared fact would hand the cross-session run
    its answer for free."""
    same = memory_recall_scenario()
    cross = memory_recall_cross_session_scenario()

    same_text = " ".join(str(t["content"]) for t in same.turns)
    cross_text = str(cross.rubric)

    assert "4821" in same_text
    assert "4821" not in cross_text
    assert "7391" in cross_text


async def test_cross_session_setup_plants_into_a_sibling_session() -> None:
    """The hook derives its session the same way the dispatch does."""
    import gateway.e2e_driver as driver
    from gateway.e2e_eval import SetupContext

    planted: list[dict[str, str]] = []

    async def _fake_plant(app: object, **kwargs: str) -> None:
        planted.append(dict(kwargs))

    original = driver.plant_turn
    driver.plant_turn = _fake_plant  # type: ignore[assignment]
    try:
        scen = memory_recall_cross_session_scenario()
        assert scen.setup is not None
        await scen.setup(SetupContext(app=object(), session_id="e2e-recall-0"))
    finally:
        driver.plant_turn = original  # type: ignore[assignment]

    assert planted[0]["session_id"] == "e2e-recall-0-a"
    assert "7391" in planted[0]["user_message"]


def test_cross_session_scenario_declares_its_prerequisite() -> None:
    scen = memory_recall_cross_session_scenario()
    assert scen.requires_tools == ("memory_search",)
    assert "embedding_alias" in scen.requires_hint


def test_cross_session_plants_the_fact_instead_of_speaking_it() -> None:
    """The model must not create the precondition: every model tried
    stores a stated fact with learn_add, and lessons cross sessions, so
    retrieval would never be exercised."""
    scen = memory_recall_cross_session_scenario()

    assert scen.setup is not None
    assert len(scen.turns) == 1  # only the recall question
    assert scen.turns[0]["session"] == "b"
    assert "4821" not in str(scen.turns[0]["content"])


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
        "cron_fires",
        "notify",
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


# --------------------------------------------------------------- notify


def test_notify_passes_when_a_message_was_actually_delivered() -> None:
    """Delivery is the agent_message event, not the reply text."""
    scen = notify_scenario(keyword="basting")

    res = scen.outcome_assert(
        _traj(
            reply="Done, sent it.",
            snapshot={"agent_messages": [{"title": "Reminder", "body": "The roast needs basting"}]},
        )
    )

    assert res.passed


def test_notify_fails_when_the_model_only_claims_to_have_sent() -> None:
    """The failure mode worth catching: a confident reply, no delivery."""
    scen = notify_scenario(keyword="basting")

    res = scen.outcome_assert(
        _traj(reply="I've sent that to your phone!", snapshot={"agent_messages": []})
    )

    assert not res.passed
    assert "never fired" in res.reason


def test_notify_fails_when_a_message_went_out_with_the_wrong_content() -> None:
    scen = notify_scenario(keyword="basting")

    res = scen.outcome_assert(
        _traj(reply="sent", snapshot={"agent_messages": [{"title": "", "body": "hello there"}]})
    )

    assert not res.passed
    assert "none mentioning" in res.reason


# --------------------------------------------------------------- cron fires


def test_cron_fires_passes_when_the_job_ran_and_delivered() -> None:
    """A cron's notification IS its cron_completed event — the fired
    session's reply. Requiring an agent_message instead made a working
    cron read as broken on the first live run."""
    scen = cron_fires_scenario(keyword="stretch")

    res = scen.outcome_assert(
        _traj(
            snapshot={
                "event_kinds": ["cron_fired", "cron_completed"],
                "deliveries": [
                    {"kind": "cron_completed", "title": "cron", "body": "time to stretch"}
                ],
            }
        )
    )

    assert res.passed


def test_cron_fires_ignores_undelivered_bookkeeping_events() -> None:
    """cron_fired is skipped by the push pipeline, so its presence is not
    delivery — the snapshot's `deliveries` slice must exclude it."""
    scen = cron_fires_scenario(keyword="stretch")

    res = scen.outcome_assert(
        _traj(
            snapshot={
                "event_kinds": ["cron_fired", "cron_completed"],
                # A cron_fired mentioning the keyword must not count.
                "deliveries": [{"kind": "cron_completed", "title": "cron", "body": "done"}],
            }
        )
    )

    assert not res.passed


def test_cron_fires_fails_when_the_job_was_only_created() -> None:
    """The gap this scenario exists for: reminder_scenario proves a job
    was created, which is not the same as it ever running."""
    scen = cron_fires_scenario(keyword="stretch")

    res = scen.outcome_assert(_traj(snapshot={"event_kinds": ["cron_created"]}))

    assert not res.passed
    assert "never ran" in res.reason


def test_cron_fires_distinguishes_a_failed_session_from_no_fire() -> None:
    scen = cron_fires_scenario(keyword="stretch")

    res = scen.outcome_assert(
        _traj(snapshot={"event_kinds": ["cron_fired", "cron_failed"], "deliveries": []})
    )

    assert not res.passed
    assert "session failed" in res.reason


def test_cron_fires_fails_when_it_fired_but_delivered_nothing() -> None:
    scen = cron_fires_scenario(keyword="stretch")

    res = scen.outcome_assert(
        _traj(snapshot={"event_kinds": ["cron_fired", "cron_completed"], "deliveries": []})
    )

    assert not res.passed
    assert "delivered" in res.reason


def test_cron_fires_declares_a_settle_hook() -> None:
    """Without forcing a tick the scenario would have to sleep, which the
    harness never does."""
    assert cron_fires_scenario().settle is not None


def test_new_scenarios_declare_their_coverage_intent() -> None:
    assert notify_scenario().exercises_tools == ("send_message",)
    assert cron_fires_scenario().exercises_tools == ("cron_add",)
