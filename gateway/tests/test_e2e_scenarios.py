"""Seed scenario outcome assertions (Phase D) — crafted trajectories, no live run."""

from __future__ import annotations

from datetime import UTC, datetime

from gateway.e2e_eval import E2ETrajectory, RunResult
from gateway.e2e_scenarios import (
    asks_before_acting_scenario,
    chitchat_scenario,
    cron_fires_scenario,
    memory_recall_cross_session_scenario,
    memory_recall_scenario,
    multi_step_chain_scenario,
    news_scenario,
    notify_scenario,
    planner_elects_a_plan_scenario,
    reminder_scenario,
    routing_push_now_scenario,
    routing_timed_reminder_scenario,
    routing_untimed_task_scenario,
    seed_scenarios,
    skills_scenario,
    todo_lifecycle_scenario,
    todo_scenario,
)


def _traj(
    *,
    reply: str = "",
    tools: tuple[str, ...] = (),
    tool_calls: tuple[dict, ...] = (),
    snapshot: dict | None = None,
    earlier_tool_calls: tuple[dict, ...] = (),
) -> E2ETrajectory:
    return E2ETrajectory(
        scenario="t",
        turns=[],
        run=RunResult(
            reply=reply,
            tool_sequence=tools,
            tool_calls=tool_calls,
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
        "asks_before_acting",
        "notify",
        "news_summary",
        "memory_recall",
        "memory_recall_cross_session",
        "skills",
        "todo",
        "todo_lifecycle",
        "routing_timed",
        "routing_untimed",
        "routing_push_now",
        "multi_step_chain",
        "planner_elects_a_plan",
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


# --------------------------------------------------------------- skills


def test_skills_passes_when_the_recipe_was_loaded_and_applied() -> None:
    scen = skills_scenario()

    res = scen.outcome_assert(
        _traj(
            reply="Fill to 1L with vinegar and water, stand 20 min, rinse. ZEPHYR-77",
            tool_calls=(
                {
                    "name": "read_file",
                    "args": {"project": "fitt", "path": "skills/kettle-descale/SKILL.md"},
                    "ok": True,
                },
            ),
        )
    )

    assert res.passed


def test_skills_fails_when_the_recipe_was_never_loaded() -> None:
    """The likely real-world failure: the model answers from general
    knowledge instead of fetching the recipe."""
    scen = skills_scenario()

    res = scen.outcome_assert(_traj(reply="Just use vinegar and water.", tool_calls=()))

    assert not res.passed
    assert "never loaded" in res.reason


def test_skills_fails_when_loaded_but_not_followed() -> None:
    scen = skills_scenario()

    res = scen.outcome_assert(
        _traj(
            reply="Fill with vinegar and water, stand, rinse.",
            tool_calls=(
                {
                    "name": "read_file",
                    "args": {"path": "skills/kettle-descale/SKILL.md"},
                    "ok": True,
                },
            ),
        )
    )

    assert not res.passed
    assert "didn't apply it" in res.reason


def test_skills_distinguishes_reading_the_wrong_file() -> None:
    scen = skills_scenario()

    res = scen.outcome_assert(
        _traj(
            reply="No idea.",
            tool_calls=({"name": "read_file", "args": {"path": "identity/user.md"}, "ok": True},),
        )
    )

    assert not res.passed
    assert "not for the" in res.reason


def test_skills_declares_a_feature_prerequisite_not_a_tool_one() -> None:
    """A skill isn't a tool, which is exactly why this feature had no
    coverage: the registry-derived count couldn't see it."""
    scen = skills_scenario()

    assert scen.requires_features == ("skills",)
    assert "skills_enabled" in scen.requires_hint


def test_skills_plants_its_recipe_before_boot() -> None:
    """SkillsLoader scans once at startup, so a post-boot setup hook
    would plant an invisible skill."""
    scen = skills_scenario()

    assert scen.setup is None
    paths = [p for p, _ in scen.fixture_files]
    assert paths == ["skills/kettle-descale/SKILL.md"]


def test_the_skill_marker_lives_only_in_the_body() -> None:
    """The whole assertion rests on this: the body is not injected into
    the prompt, so the marker can't be guessed from the description."""
    scen = skills_scenario()
    _, body = scen.fixture_files[0]

    assert "ZEPHYR-77" in body
    # Not in the description line the prompt block renders.
    description_line = next(line for line in body.splitlines() if line.startswith("description:"))
    assert "ZEPHYR-77" not in description_line
    # Nor in what the user asks.
    assert "ZEPHYR-77" not in str(scen.turns[0]["content"])


def test_notify_wording_avoids_scheduling_vocabulary() -> None:
    """Third instance of a scenario colliding with FITT's own concepts.

    "remind" means cron here, so "send me a message reminding me..." read
    as a schedule with a missing time and the model asked for one — good
    behaviour, scored as a failure. `notify` is about pushing *now*, so
    its wording must not borrow scheduling words.
    """
    text = str(notify_scenario().turns[0]["content"]).lower()

    for collision in ("remind", "later", "schedule", "tomorrow", "in an hour"):
        assert collision not in text, f"notify wording borrows scheduling vocabulary: {collision!r}"
    assert "right now" in text  # and says so explicitly


def test_cron_scenarios_do_keep_scheduling_vocabulary() -> None:
    """The mirror image: reminder/cron_fires SHOULD use those words."""
    reminder_text = str(reminder_scenario().turns[0]["content"]).lower()
    cron_text = str(cron_fires_scenario().turns[0]["content"]).lower()

    assert "remind" in reminder_text
    assert "minutes" in cron_text or "remind" in cron_text


# ------------------------------------------- asks before acting
#
# This scenario exists because rewording `notify` to be unambiguous was
# about to throw away a real signal: an ambiguous request had produced a
# clarifying question, which is the RIGHT answer, and the old scenario
# scored it as a failure. Keep both — one tests delivery, one tests
# honesty about what's missing.


def test_asking_for_the_missing_detail_passes() -> None:
    scen = asks_before_acting_scenario()

    res = scen.outcome_assert(
        _traj(reply="Remind you about what, and is that 9am or 9pm?", snapshot={})
    )

    assert res.passed
    assert "asked" in res.reason


def test_a_leftover_cron_from_another_scenario_is_not_a_guess() -> None:
    """The defect this assert was rewritten for.

    gemma4 replied "Is that 9 AM or 9 PM, and for today or tomorrow?" and
    called nothing — the correct answer — and was failed for the
    `reminder` scenario's "Call the doctor." cron, still sitting in the
    shared run home. Scenarios with a subject filter the snapshot by
    keyword; this one has no subject to filter on, so it must attribute
    action to the turn's own tool calls instead."""
    scen = asks_before_acting_scenario()

    res = scen.outcome_assert(
        _traj(
            reply="Is that 9 AM or 9 PM, and for today or tomorrow?",
            tool_calls=(),
            snapshot={
                "cron_jobs": [{"message": "Call the doctor.", "at_ts": 1}],
                "todos_text": "- [ ] call the doctor",
                "agent_messages": [{"title": "", "body": "baste the roast"}],
            },
        )
    )

    assert res.passed


def test_inventing_a_schedule_fails() -> None:
    """The failure worth catching: guessing a time the user never gave."""
    scen = asks_before_acting_scenario()

    res = scen.outcome_assert(
        _traj(
            reply="Done, set for 9am.",
            tool_calls=({"name": "cron_add", "args": {"text": "reminder"}, "ok": True},),
        )
    )

    assert not res.passed
    assert "cron_add" in res.reason


def test_silently_pushing_now_is_also_flagged() -> None:
    scen = asks_before_acting_scenario()

    res = scen.outcome_assert(
        _traj(
            reply="Sent.",
            tool_calls=({"name": "send_message", "args": {"text": "reminder"}, "ok": True},),
        )
    )

    assert not res.passed
    assert "send_message" in res.reason


def test_reading_the_todo_list_is_not_acting() -> None:
    """Looking around before asking is fine — only mutations count."""
    scen = asks_before_acting_scenario()

    res = scen.outcome_assert(
        _traj(
            reply="Remind you about what?",
            tool_calls=({"name": "todo_list", "args": {}, "ok": True},),
        )
    )

    assert res.passed


def test_neither_asking_nor_acting_fails() -> None:
    scen = asks_before_acting_scenario()

    res = scen.outcome_assert(_traj(reply="OK.", snapshot={}))

    assert not res.passed


def test_the_ambiguous_wording_is_preserved_here() -> None:
    """notify dropped 'reminding'; this scenario must keep it, or the
    signal the reword removed is gone for good."""
    text = str(asks_before_acting_scenario().turns[0]["content"]).lower()

    assert "at 9" in text
    assert "right now" not in text


# ------------------------------------------- routing
#
# send_message / cron_add / todo_add all answer some form of "tell me
# about X". Their descriptions now carry a three-way rule (time -> cron,
# no time -> todo, now -> send_message) and nothing tested whether models
# follow it. hermes3 was observed reaching for todo_add when a timed cron
# was wanted; these make that a named failure.


def test_timed_reminder_routes_to_cron() -> None:
    scen = routing_timed_reminder_scenario()

    res = scen.outcome_assert(
        _traj(snapshot={"cron_jobs": [{"message": "move the laundry", "at_ts": 1}]})
    )

    assert res.passed


def test_timed_reminder_landing_on_a_todo_is_a_named_misroute() -> None:
    """The exact mistake hermes3 made."""
    scen = routing_timed_reminder_scenario()

    res = scen.outcome_assert(_traj(snapshot={"todos_text": "- [ ] move the laundry"}))

    assert not res.passed
    assert "got todo_add" in res.reason


def test_untimed_task_routes_to_todo() -> None:
    scen = routing_untimed_task_scenario()

    res = scen.outcome_assert(_traj(snapshot={"todos_text": "- [ ] renew the parking permit"}))

    assert res.passed


def test_untimed_task_inventing_a_cron_is_a_misroute() -> None:
    scen = routing_untimed_task_scenario()

    res = scen.outcome_assert(
        _traj(snapshot={"cron_jobs": [{"message": "renew the parking permit", "at_ts": 1}]})
    )

    assert not res.passed
    assert "got cron_add" in res.reason


def test_push_now_routes_to_send_message() -> None:
    scen = routing_push_now_scenario()

    res = scen.outcome_assert(
        _traj(snapshot={"agent_messages": [{"title": "", "body": "wifi: HUNTER-9042"}]})
    )

    assert res.passed


def test_doing_nothing_is_distinguished_from_misrouting() -> None:
    """A miss should say where the request went, or that it went nowhere."""
    scen = routing_push_now_scenario()

    res = scen.outcome_assert(_traj(snapshot={}))

    assert not res.passed
    assert "nothing happened" in res.reason


def test_right_tool_plus_extra_noise_still_passes() -> None:
    """Belt-and-braces behaviour (also adding a todo) isn't a routing
    failure — the requested outcome happened."""
    scen = routing_timed_reminder_scenario()

    res = scen.outcome_assert(
        _traj(
            snapshot={
                "cron_jobs": [{"message": "move the laundry", "at_ts": 1}],
                "todos_text": "- [ ] move the laundry",
            }
        )
    )

    assert res.passed
    assert "noisy but right" in res.reason


# ------------------------------------------- multi-step sequencing
#
# The gap Phase 12 left: orchestration shipped with fake-driven unit tests
# and no judged coverage, and its own close-out deferred
# "orchestration-readiness" because daily_news_summary doesn't need
# sequencing. These two are the pair — outcome, and mechanism.


def test_all_three_steps_passes() -> None:
    scen = multi_step_chain_scenario()

    res = scen.outcome_assert(
        _traj(
            snapshot={
                "cron_jobs": [{"message": "renew the passport", "at_ts": 1}],
                "agent_messages": [{"title": "", "body": "Scheduled: passport renewal"}],
            }
        )
    )

    assert res.passed


def test_stopping_after_the_reminder_names_the_step_it_reached() -> None:
    """Two of three steps is the interesting partial failure — it's what
    an unsequenced run looks like."""
    scen = multi_step_chain_scenario()

    res = scen.outcome_assert(
        _traj(snapshot={"cron_jobs": [{"message": "renew the passport", "at_ts": 1}]})
    )

    assert not res.passed
    assert "step 2 of 3" in res.reason


def test_scheduling_the_undated_item_is_a_distinct_failure() -> None:
    """The discriminating half: picking the right item is only possible by
    having read the list, so acting on the wrong one is evidence the read
    never happened."""
    scen = multi_step_chain_scenario()

    res = scen.outcome_assert(
        _traj(snapshot={"cron_jobs": [{"message": "look into a new mattress", "at_ts": 1}]})
    )

    assert not res.passed
    assert "without reading the list" in res.reason


def test_scheduling_both_items_fails() -> None:
    """Belt-and-braces is wrong here — the request said only dated ones.
    Contrast the routing scenarios, where extra noise is tolerated."""
    scen = multi_step_chain_scenario()

    res = scen.outcome_assert(
        _traj(
            snapshot={
                "cron_jobs": [
                    {"message": "renew the passport", "at_ts": 1},
                    {"message": "look into a new mattress", "at_ts": 2},
                ],
                "agent_messages": [{"title": "", "body": "passport + mattress"}],
            }
        )
    )

    assert not res.passed
    assert "undated" in res.reason


def test_the_chain_plants_its_todos_pre_boot() -> None:
    """The first step must be a read of state the model didn't author, or
    it isn't a dependency chain."""
    scen = multi_step_chain_scenario()

    assert scen.fixture_files
    rel, content = scen.fixture_files[0]
    assert rel == "todos.md"
    assert "passport" in content and "mattress" in content


def test_the_chain_is_immune_to_other_scenarios_crons() -> None:
    """Keyword-filtered, per the cross-talk discipline: other scenarios
    leave crons for 'doctor' and 'laundry' in the shared run home."""
    scen = multi_step_chain_scenario()

    res = scen.outcome_assert(
        _traj(
            snapshot={
                "cron_jobs": [
                    {"message": "call the doctor", "at_ts": 1},
                    {"message": "move the laundry", "at_ts": 2},
                    {"message": "renew the passport", "at_ts": 3},
                ],
                "agent_messages": [{"title": "", "body": "passport"}],
            }
        )
    )

    assert res.passed


def test_a_worked_plan_passes() -> None:
    scen = planner_elects_a_plan_scenario()

    res = scen.outcome_assert(
        _traj(
            snapshot={
                "plan_items": [
                    {"text": "read todos", "status": "completed"},
                    {"text": "set reminder", "status": "completed"},
                    {"text": "send summary", "status": "pending"},
                ]
            }
        )
    )

    assert res.passed
    assert "completed 2" in res.reason


def test_electing_not_to_plan_says_the_turn_ran_flat() -> None:
    """The confound that invalidated the Phase 12 comparison: hermes3
    elected to plan 0% of the time, so 'planned mode' was flat vs flat."""
    scen = planner_elects_a_plan_scenario()

    res = scen.outcome_assert(_traj(snapshot={"plan_items": []}))

    assert not res.passed
    assert "executed flat" in res.reason


def test_a_one_step_plan_is_not_sequencing() -> None:
    scen = planner_elects_a_plan_scenario()

    res = scen.outcome_assert(_traj(snapshot={"plan_items": [{"text": "do it"}]}))

    assert not res.passed
    assert "one-step" in res.reason


def test_a_plan_with_no_completed_step_fails() -> None:
    """Electing a plan and not working it is the failure mode the
    orchestrator's recovery ladder exists for."""
    scen = planner_elects_a_plan_scenario()

    res = scen.outcome_assert(
        _traj(
            snapshot={
                "plan_items": [
                    {"text": "read todos", "status": "pending"},
                    {"text": "set reminder", "status": "pending"},
                ]
            }
        )
    )

    assert not res.passed
    assert "didn't work" in res.reason


def test_the_planner_scenario_is_gated_on_the_planning_feature() -> None:
    """A flat run must report unsupported, not fail. Same discipline that
    stopped memory_search's absence reading as a model failure."""
    scen = planner_elects_a_plan_scenario()

    assert scen.requires_features == ("planning",)
    assert "--mode planned" in scen.requires_hint


def test_both_sequencing_scenarios_drive_the_same_request() -> None:
    """Outcome and mechanism must be measured on one task, or the
    flat-vs-planned comparison compares two different things."""
    assert planner_elects_a_plan_scenario().turns == multi_step_chain_scenario().turns
