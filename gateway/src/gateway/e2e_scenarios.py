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


def _memory_assert(keyword: str):  # type: ignore[no-untyped-def]
    def _a(traj: E2ETrajectory) -> OutcomeResult:
        called = any("memory_search" in t for t in traj.run.tool_sequence)
        recalled = keyword.lower() in traj.run.reply.lower()
        if not called:
            return OutcomeResult(False, "memory_search did not fire on the recall turn")
        if not recalled:
            return OutcomeResult(False, f"reply didn't surface the recalled fact {keyword!r}")
        return OutcomeResult(True, f"memory_search fired and {keyword!r} was recalled")

    return _a


def memory_recall_scenario(
    *,
    fact: str = "The deploy uses docker compose on the hub.",
    question: str = "How do we deploy the hub again?",
    keyword: str = "docker compose",
) -> TaskScenario:
    return TaskScenario(
        name="memory_recall",
        turns=[
            {"role": "user", "content": f"Note this for later: {fact}"},
            {"role": "user", "content": question},
        ],
        outcome_assert=_memory_assert(keyword),
        rubric=(
            f"Is the answer grounded in the earlier fact the user stated ({fact!r})? "
            "It should reflect that fact, not guess or say it doesn't know."
        ),
    )


def seed_scenarios() -> list[TaskScenario]:
    """The scenarios available today (todo lands in Phase E)."""
    return [reminder_scenario(), news_scenario(), memory_recall_scenario()]
