"""Pin the scenario premises that live in the tool descriptions.

A scenario is only a fair test if the behaviour it demands is behaviour
FITT actually advertises to the model. Three e2e scenarios rest on
claims made *in the tool descriptions* — the send/cron/todo routing
rule — and until this file existed those claims were pinned nowhere.

The gap was not hypothetical. `asks_before_acting` was written around
"Send me a message reminding me that ..." precisely because that wording
was unresolvable. Then `send_message`'s description grew "(a) the user
asks for one now — 'text me X' ..." and pushing immediately became the
*correct* reading, so the scenario was silently asserting a failure for
behaviour the prompt now endorses. Nothing failed; the objective check
just quietly started disagreeing with the judge.

That's the test smell these tests remove: an **unpinned premise** — a
test depending on a property of production text that lives nowhere and
is never checked. Change a description and something here fails, in the
same commit, naming the scenario it invalidates.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from gateway.e2e_scenarios import (
    _ACTING_TOOLS,
    asks_before_acting_scenario,
    deadline_sweep_scenario,
    routing_push_now_scenario,
    routing_timed_reminder_scenario,
    routing_untimed_task_scenario,
)
from gateway.tools import build_core_tool_registry
from gateway.tools.cron_tools import build_cron_tools
from gateway.tools.send_message import build_send_message_tool
from gateway.tools.todo_tools import build_todo_tools

from ._fixtures import build_test_config

ROUTING_TOOLS = ("send_message", "cron_add", "todo_add")

# Placeholders in an advertised example phrase ('text me X'): they stand
# for whatever the user actually says, so they can't be matched.
_PLACEHOLDERS = {"x", "y", "z"}


@pytest.fixture
def descriptions() -> dict[str, str]:
    """The *live* descriptions the model is shown.

    Built from the real builders rather than copied, so this file can't
    drift into testing a stale snapshot of the prompt. `send_message`
    isn't in the core registry (it needs runtime handles), hence the
    separate construction."""
    d = {t.name: t.description for t in build_cron_tools() + build_todo_tools()}
    d["send_message"] = build_send_message_tool().description
    return {k: v for k, v in d.items() if k in ROUTING_TOOLS}


def _quoted_examples(description: str) -> list[str]:
    """Example user phrasings a description advertises in single quotes.

    The lookarounds are load-bearing: `send_message`'s description also
    contains possessives ("the user's phone", "a silent cron's result").
    A naive pair-the-quotes regex counts those as delimiters, and an odd
    number of them shifts every subsequent pair by one — silently
    yielding garbage fragments instead of the examples. Requiring a
    non-word character outside each quote excludes possessives."""
    return [m.lower() for m in re.findall(r"(?<!\w)'([^']+?)'(?!\w)", description)]


def _matchable_runs(example: str) -> list[str]:
    """The literal word-runs of an example, placeholders removed.

    'text me X' -> ['text me'] and 'add X to my todos' -> ['add', 'to my
    todos']: a turn matches the example only if every run appears in it,
    which keeps 'remind me to Z' from matching a bare "remind me"."""
    runs: list[str] = []
    current: list[str] = []
    for word in example.split():
        if word.strip(".,") in _PLACEHOLDERS:
            if current:
                runs.append(" ".join(current))
                current = []
            continue
        current.append(word)
    if current:
        runs.append(" ".join(current))
    return runs


def _advertises(description: str, turn: str) -> list[str]:
    """Which of a description's example phrasings this turn matches."""
    text = turn.lower()
    return [
        ex
        for ex in _quoted_examples(description)
        if all(run in text for run in _matchable_runs(ex))
    ]


def _turn_text(scenario) -> str:  # type: ignore[no-untyped-def]
    return " ".join(str(t["content"]) for t in scenario.turns)


def test_example_extraction_survives_possessives(descriptions: dict[str, str]) -> None:
    """Guard the helper, or every test below passes vacuously.

    `send_message`'s description mixes advertised examples with
    possessive apostrophes ("the user's phone"). If extraction mis-pairs
    them it returns fragments rather than examples, and the checks that
    look for a phrase simply stop finding anything — passing where they
    should fail."""
    examples = _quoted_examples(descriptions["send_message"])

    assert examples == ["text me x", "send that to my phone", "message me the summary"]


def test_the_detector_fires_on_a_claim_and_not_on_a_near_miss() -> None:
    """Show the check below can actually fail, and won't cry wolf.

    Without this, "no description claims this wording" is
    indistinguishable from "the matcher never matches anything"."""
    turn = "Remind me at 9."

    assert _advertises("... use this when the user says 'remind me at X' ...", turn)
    # 'remind me to Z' must NOT claim "remind me at 9" — placeholders
    # can't absorb the word that distinguishes the two.
    assert not _advertises("... 'remind me to Z' (with no specific time) ...", turn)


def test_each_routing_tool_names_the_other_two(descriptions: dict[str, str]) -> None:
    """The triangle has to be closed for the routing scenarios to be fair.

    The routing scenarios grade a three-way choice. If a description
    stops naming its two neighbours, the model is being graded on a
    boundary it was never told about — the scenarios would still "work",
    they'd just be measuring guesswork."""
    for name, desc in descriptions.items():
        others = [o for o in ROUTING_TOOLS if o != name]
        missing = [o for o in others if o not in desc]
        assert not missing, (
            f"{name} no longer names {missing}; routing scenarios lose their premise"
        )


def test_the_timed_and_untimed_boundary_is_documented(descriptions: dict[str, str]) -> None:
    """`routing_timed` expects cron and `routing_untimed` expects todo
    solely because the prompt says so. Pin the clause, not the vibe."""
    assert "a time given -> cron_add" in descriptions["cron_add"]
    assert "no time -> todo_add" in descriptions["cron_add"]
    assert "no time -> todo_add" in descriptions["todo_add"]
    assert "a time ('tomorrow at 9', 'in 10 minutes') -> cron_add" in descriptions["todo_add"]


def test_push_now_uses_a_phrasing_send_message_advertises(descriptions: dict[str, str]) -> None:
    """`routing_push_now` exists because the description claims this case.

    "Text me X" is only a fair expectation while `send_message` lists it
    as a trigger. Drop that clause and this scenario becomes a test of
    undocumented behaviour."""
    turn = _turn_text(routing_push_now_scenario())

    matched = _advertises(descriptions["send_message"], turn)

    assert matched, f"send_message no longer advertises anything matching {turn!r}"


def test_the_ambiguous_scenario_claims_no_advertised_phrasing(
    descriptions: dict[str, str],
) -> None:
    """`asks_before_acting` must not use wording the prompt resolves.

    This is the specific regression that already happened once, in
    reverse: the scenario's wording stayed put while a description grew
    a clause that claimed it. Substring matching is a floor, not a proof
    of ambiguity — it cannot certify that a request is genuinely
    unresolvable. What it does do is fail, loudly and in the same
    commit, the day a description starts advertising the exact phrasing
    this scenario needs left unclaimed."""
    turn = _turn_text(asks_before_acting_scenario())

    claimed = {
        name: _advertises(desc, turn)
        for name, desc in descriptions.items()
        if _advertises(desc, turn)
    }

    assert not claimed, (
        f"asks_before_acting says {turn!r}, which {claimed} now advertises as its own "
        "case — the request is no longer ambiguous and the scenario needs rewording"
    )


def test_routing_scenarios_expect_tools_that_carry_the_rule(descriptions: dict[str, str]) -> None:
    """Every routing scenario's target is a tool that states the rule.

    Guards the other direction: a scenario pointed at a tool outside the
    triangle would be graded against a boundary nothing documents."""
    scenarios = (
        routing_timed_reminder_scenario(),
        routing_untimed_task_scenario(),
        routing_push_now_scenario(),
    )
    for scen in scenarios:
        assert scen.exercises_tools, f"{scen.name} declares no coverage intent"
        for tool in scen.exercises_tools:
            assert tool in descriptions, (
                f"{scen.name} targets {tool}, which carries no routing rule"
            )
            assert (
                "Three-way rule" in descriptions[tool]
                or "NOT for scheduling" in (descriptions[tool])
            ), f"{tool} no longer states its boundary"


# ------------------------------------------- the acting-tool list
#
# `asks_before_acting` can't filter the shared end state by a keyword (its
# premise is that no subject was given), so it decides "did FITT act?"
# from the turn's own tool calls, matched against a hand-written list of
# names. A rename would make the check silently stop noticing action —
# the same unpinned-premise smell as above, one layer down.


def test_acting_tools_are_all_really_registered(tmp_path: Path) -> None:
    registered = set(build_core_tool_registry(build_test_config(tmp_path)).list_names())
    # send_message needs runtime handles, so it isn't in the core set.
    registered.add(build_send_message_tool().name)

    unknown = sorted(_ACTING_TOOLS - registered)

    assert not unknown, f"_ACTING_TOOLS names tools that don't exist: {unknown}"


def test_every_tool_the_routing_scenarios_expect_counts_as_acting() -> None:
    """Otherwise a routing scenario could 'pass' a turn the honesty
    scenario would call inaction."""
    for scen in (
        routing_timed_reminder_scenario(),
        routing_untimed_task_scenario(),
        routing_push_now_scenario(),
    ):
        assert set(scen.exercises_tools) <= _ACTING_TOOLS, scen.name


# ------------------------------------------- scenarios must not fight
#
# `asks_before_acting` rewards asking when a required detail is missing.
# Any scenario that asserts a side effect therefore has to supply the
# details that action needs — otherwise the suite pays a model for asking
# in one scenario and penalises it for the same judgement in another, and
# the loser is decided by which assert happens to run.
#
# This is not hypothetical: `deadline_sweep` scored 0 of 3 on both loop
# modes because its request never named a reminder lead time. gemma4
# identified exactly the right three items, proposed firing two days early,
# and asked before creating three crons. Correct behaviour, failed twice.

# Requests that demand a side effect must grant permission, because the
# harness has no human to confirm with. Phrases that do that.
_GO_AHEAD = ("go ahead", "no need to check", "don't ask", "just do it")


def test_the_ask_scenario_and_the_act_scenarios_do_not_overlap() -> None:
    """The honesty scenario must be the ONLY one whose request is missing a
    detail the action needs. Pinned by construction: it asks for a reminder
    with no subject and a partial time, and no acting scenario may share
    that shape."""
    ask = _turn_text(asks_before_acting_scenario())

    # The premise of the honesty scenario, restated so a reword can't
    # quietly remove it.
    assert "remind" in ask.lower()
    assert not any(p in ask.lower() for p in _GO_AHEAD), (
        "asks_before_acting granted permission to act, which destroys its "
        "own premise — asking is only correct while a detail is missing"
    )


def test_the_multi_item_action_scenario_grants_permission() -> None:
    """Three crons is a big enough side effect that a well-behaved model
    checks first. If the request doesn't pre-authorise it, the scenario
    measures politeness rather than completeness."""
    text = _turn_text(deadline_sweep_scenario()).lower()

    assert any(p in text for p in _GO_AHEAD), (
        "deadline_sweep asserts three crons get created but never says to go "
        "ahead — a model that asks first is behaving correctly and will fail it"
    )
    assert "two days before" in text, (
        "the reminder lead time is unstated, so the model has to invent it — "
        "which is exactly what asks_before_acting rewards asking about"
    )
