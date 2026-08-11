"""Judged end-to-end harness — seed scenarios (Phase D).

Each scenario is a natural-language request + a deterministic
``outcome_assert`` (the "did FITT actually do it" check) + an optional
judge rubric (the fuzzy reply-quality check). The assertions read the
side-effect snapshot / tool_sequence / reply from the trajectory — no
LLM — so they're unit-testable against crafted trajectories.

Seed set: **reminder** (cron one-shot), **news** (web_search +
substantive summary, quality-judged), **memory_recall** (Phase 9 —
did the model call memory_search and surface the earlier fact). The
**todo** scenario lands with the todo feature in Phase E.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .e2e_eval import E2ETrajectory, OutcomeResult, TaskScenario

_REMINDER_WINDOW_S = 36 * 3600  # a "tomorrow ~9am" reminder is within ~1.5 days


def _reminder_assert(subject: str):  # type: ignore[no-untyped-def]
    def _a(traj: E2ETrajectory) -> OutcomeResult:
        now = datetime.now(UTC).timestamp()
        for j in traj.snapshot.get("cron_jobs", []):
            if j.get("schedule_kind") == "at" and j.get("at_ts") is not None:
                at = float(j["at_ts"])
                mentions = subject.lower() in str(j.get("message", "")).lower()
                if now < at <= now + _REMINDER_WINDOW_S and mentions:
                    return OutcomeResult(
                        True,
                        f"one-shot cron mentioning {subject!r} set ~{(at - now) / 3600:.0f}h out",
                    )
        return OutcomeResult(False, f"no future one-shot cron mentioning {subject!r}")

    return _a


def reminder_scenario(*, subject: str = "doctor") -> TaskScenario:
    return TaskScenario(
        name="reminder",
        turns=[{"role": "user", "content": f"Remind me to call the {subject} tomorrow at 9am."}],
        outcome_assert=_reminder_assert(subject),
        rubric=(
            f"Did the assistant confirm it set a reminder to call the {subject} for "
            "9am tomorrow (a specific one-shot time, not a vague 'I'll remind you')?"
        ),
    )


def _news_assert() -> object:
    def _a(traj: E2ETrajectory) -> OutcomeResult:
        searched = any("web_search" in t for t in traj.run.tool_sequence)
        if not searched:
            return OutcomeResult(False, "web_search did not fire (may have refused/narrated)")
        if len(traj.run.reply.strip()) < 80:
            return OutcomeResult(False, "reply too short to be a real summary")
        return OutcomeResult(True, "web_search fired and the reply is substantive")

    return _a


def news_scenario(*, topic: str = "technology") -> TaskScenario:
    return TaskScenario(
        name="news_summary",
        turns=[{"role": "user", "content": f"Give me a short news summary about {topic} today."}],
        outcome_assert=_news_assert(),  # type: ignore[arg-type]
        rubric=(
            f"Is the summary grounded in real fetched results, on-topic for {topic!r}, and "
            "substantive — NOT a refusal like 'I can't access real-time data' or a generic "
            "non-answer?"
        ),
    )


def _recall_assert(keyword: str, *, require_search: bool):  # type: ignore[no-untyped-def]
    """Grade recall on the *outcome* — did the fact come back — and only
    additionally on the mechanism when the mechanism is truly required.

    Requiring ``memory_search`` within a single session was wrong: the
    fact is one turn back in the same conversation, so history already
    carries it and a correct model answers directly. The old check
    punished the right behaviour and, worse, looked like a model defect.
    ``memory_search`` is for *cross-session* recall, so only the
    cross-session scenario demands it."""

    def _a(traj: E2ETrajectory) -> OutcomeResult:
        recalled = keyword.lower() in traj.run.reply.lower()
        called = any("memory_search" in t for t in traj.run.tool_sequence)
        if not require_search:
            if not recalled:
                return OutcomeResult(False, f"reply didn't surface the recalled fact {keyword!r}")
            how = "via memory_search" if called else "from conversation history"
            return OutcomeResult(True, f"recalled {keyword!r} {how}")

        # Cross-session: retrieval should be the only path — unless an
        # earlier turn stored the fact as a *lesson*, which is injected
        # into every system prompt regardless of session. Then the run
        # simply didn't test retrieval, and the model deserves neither
        # credit nor blame.
        learned = [c for c in traj.run.earlier_tool_calls if str(c.get("name", "")) == "learn_add"]
        if not called and learned and recalled:
            return OutcomeResult(
                False,
                f"recalled {keyword!r}, but an earlier learn_add put it in the "
                "global [Learned corrections] block, so it reached the model "
                "without retrieval — this run didn't exercise memory_search",
                inconclusive=True,
            )
        if not called:
            return OutcomeResult(
                False,
                "memory_search did not fire, and neither history nor a lesson "
                "carried the fact across sessions — the fact was unreachable",
            )
        if not recalled:
            return OutcomeResult(False, f"reply didn't surface the recalled fact {keyword!r}")
        return OutcomeResult(True, f"recalled {keyword!r} via memory_search")

    return _a


# A recall fact has to avoid FITT's own vocabulary. The original
# ("the deploy uses docker compose on the hub" / "how do we deploy the
# hub again?") collided with the project registry on every model tried:
# "hub" reads as a project name and "deploy" as an actionable request,
# so models went to project_shell / spec_list / list_directory and
# asked which project to register. They had the fact and still failed —
# the scenario was measuring tool routing, not recall.
_RECALL_FACT = "My bike lock combination is 4821."
_RECALL_QUESTION = "What's my bike lock combination again?"
_RECALL_KEYWORD = "4821"


def memory_recall_scenario(
    *,
    fact: str = _RECALL_FACT,
    question: str = _RECALL_QUESTION,
    keyword: str = _RECALL_KEYWORD,
) -> TaskScenario:
    """Same-session recall: state a fact, then ask for it one turn later.

    Tests that FITT's history injection puts the earlier turn in front
    of the model. The mechanism is free — history, a lesson, or
    memory_search all count — because only the outcome matters here."""
    return TaskScenario(
        name="memory_recall",
        turns=[
            {"role": "user", "content": f"Note this for later: {fact}"},
            {"role": "user", "content": question},
        ],
        outcome_assert=_recall_assert(keyword, require_search=False),
        rubric=(
            f"Is the answer grounded in the earlier fact the user stated ({fact!r})? "
            "It should reflect that fact, not guess or say it doesn't know."
        ),
    )


def memory_recall_cross_session_scenario(
    *,
    fact: str = _RECALL_FACT,
    question: str = _RECALL_QUESTION,
    keyword: str = _RECALL_KEYWORD,
) -> TaskScenario:
    """Cross-session recall: state the fact in one session, ask in another.

    This is what ``memory_search`` is actually for (Phase 9). History
    cannot carry the fact across a session boundary, so retrieval is the
    only path — which makes requiring the tool call fair here, unlike in
    the same-session scenario."""
    return TaskScenario(
        name="memory_recall_cross_session",
        turns=[
            # Deliberately NOT "note this for later" — that phrasing
            # invites learn_add, whose global lessons block would carry
            # the fact across the session boundary and leave retrieval
            # untested (the run then reports inconclusive). A plain
            # aside is more likely to be persisted as ordinary history
            # and reachable only through the index.
            {
                "role": "user",
                "content": f"By the way, {fact[0].lower() + fact[1:]}",
                "session": "a",
            },
            {"role": "user", "content": question, "session": "b"},
        ],
        outcome_assert=_recall_assert(keyword, require_search=True),
        rubric=(
            f"Does the answer surface the fact stated in an earlier session ({fact!r})? "
            "It should recall it, not guess and not claim it has no record."
        ),
        # memory_search only exists when memory.embedding_alias is
        # configured; without this the scenario would score a
        # switched-off feature as a model failure.
        requires_tools=("memory_search",),
        requires_hint=(
            "set memory.enabled: true and bind memory.embedding_alias to an "
            "alias backed by an embedding model (e.g. nomic-embed-text on "
            "ollama) in config.yaml"
        ),
    )


def _todo_assert(item: str):  # type: ignore[no-untyped-def]
    def _a(traj: E2ETrajectory) -> OutcomeResult:
        text = str(traj.snapshot.get("todos_text", ""))
        if item.lower() in text.lower():
            return OutcomeResult(True, f"todos.md contains {item!r}")
        return OutcomeResult(False, f"todos.md does not contain {item!r}")

    return _a


def todo_scenario(*, item: str = "call the doctor") -> TaskScenario:
    return TaskScenario(
        name="todo",
        turns=[{"role": "user", "content": f"Add '{item}' to my todo list."}],
        outcome_assert=_todo_assert(item),
        rubric=f"Did the assistant confirm it added '{item}' to the todo list?",
    )


def _chitchat_assert() -> object:
    def _a(traj: E2ETrajectory) -> OutcomeResult:
        reply = traj.run.reply.strip()
        if not reply:
            return OutcomeResult(False, "empty reply")
        if traj.run.tool_sequence:
            return OutcomeResult(
                False, f"called tool(s) on plain chitchat: {list(traj.run.tool_sequence)}"
            )
        return OutcomeResult(True, "replied conversationally with no tool call")

    return _a


def chitchat_scenario() -> TaskScenario:
    """The easiest possible turn: a friendly greeting, no tool needed. The
    objective check is that a reply came back AND no tool fired (a weak
    model shouldn't hallucinate a tool call for small talk); the rubric
    judges whether it's a coherent, friendly reply."""
    return TaskScenario(
        name="chitchat",
        turns=[{"role": "user", "content": "Hey, how's it going today?"}],
        outcome_assert=_chitchat_assert(),  # type: ignore[arg-type]
        rubric=(
            "Is the reply a friendly, coherent conversational response — not a "
            "refusal, not empty, not an error message, and not a spurious tool "
            "call or JSON blob?"
        ),
    )


def _todo_done_assert(item: str):  # type: ignore[no-untyped-def]
    def _a(traj: E2ETrajectory) -> OutcomeResult:
        text = str(traj.snapshot.get("todos_text", ""))
        for line in text.splitlines():
            if item.lower() in line.lower() and "[x]" in line.lower():
                return OutcomeResult(True, f"todos.md has {item!r} marked done")
        if item.lower() in text.lower():
            return OutcomeResult(False, f"{item!r} is present but not marked done")
        return OutcomeResult(False, f"todos.md does not contain {item!r}")

    return _a


def todo_lifecycle_scenario(*, item: str = "buy milk") -> TaskScenario:
    """Two turns: add an item, then mark it done. Objective = the item
    exists in todos.md AND is checked off (tests todo_add + todo_done, not
    just the add). Uses a distinct item from ``todo_scenario`` so the two
    don't collide in a shared run's todos.md."""
    return TaskScenario(
        name="todo_lifecycle",
        turns=[
            {"role": "user", "content": f"Add '{item}' to my todo list."},
            {"role": "user", "content": f"I've done that now — mark '{item}' as done."},
        ],
        outcome_assert=_todo_done_assert(item),
        rubric=(
            f"Across the two turns, did the assistant add '{item}' and then "
            "confirm it marked it done/complete?"
        ),
    )


def seed_scenarios() -> list[TaskScenario]:
    """The scenarios available today."""
    return [
        chitchat_scenario(),
        reminder_scenario(),
        news_scenario(),
        memory_recall_scenario(),
        memory_recall_cross_session_scenario(),
        todo_scenario(),
        todo_lifecycle_scenario(),
    ]
