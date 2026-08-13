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

import contextlib
from datetime import UTC, datetime

from .e2e_eval import (
    E2ETrajectory,
    OutcomeAssert,
    OutcomeResult,
    SetupContext,
    TaskScenario,
)

_REMINDER_WINDOW_S = 36 * 3600  # a "tomorrow ~9am" reminder is within ~1.5 days

_ACTING_TOOLS = frozenset(
    {
        "cron_add",
        "cron_update",
        "cron_remove",
        "cron_pause",
        "cron_resume",
        "todo_add",
        "todo_done",
        "todo_remove",
        "send_message",
        "learn_add",
        "learn_remove",
        "write_file",
        "edit_file",
    }
)
"""Tools that change something the user would notice.

Used where a scenario must tell "did FITT act?" from "did FITT ask?" and
cannot filter the end-state snapshot by a keyword — a read like
`todo_list` or `cron_list` is not acting."""


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

        # Cross-session: retrieval should be the only path — unless the
        # fact is sitting in the global [Learned corrections] block,
        # which is injected into every system prompt regardless of
        # session. Read the actual lessons state rather than inferring
        # from this scenario's tool calls: the leak that bit us came from
        # a *different* scenario earlier in the same run (they share one
        # run home), so a within-scenario check couldn't see it.
        lessons_text = str(traj.snapshot.get("lessons_text", ""))
        leaked = keyword.lower() in lessons_text.lower()
        if not called and leaked and recalled:
            return OutcomeResult(
                False,
                f"recalled {keyword!r}, but it is in the global "
                "[Learned corrections] block, so it reached the model without "
                "retrieval — this run didn't exercise memory_search",
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

# The cross-session scenario needs a DIFFERENT fact. All scenarios in a
# run share one home, and the same-session scenario above legitimately
# stores its fact with learn_add — which lands in the global lessons
# block and would hand the cross-session run its answer for free. Two
# facts, no coupling.
_CROSS_FACT = "My gym locker number is 7391."
_CROSS_QUESTION = "What's my gym locker number again?"
_CROSS_KEYWORD = "7391"


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


def _plant_prior_session_fact(fact: str):  # type: ignore[no-untyped-def]
    """Setup hook: put ``fact`` in a *different* session's history and
    index, with no model call.

    Driving that first turn through the model doesn't work here: every
    model tried stores a stated personal fact with ``learn_add``, and
    lessons are injected into every system prompt regardless of session,
    so the recall turn gets the fact for free and retrieval is never
    exercised. Planting it directly is the only way to make
    ``memory_search`` the sole path to it."""

    async def _setup(ctx: SetupContext) -> None:
        # Local import: keeps this module cheap to import (the driver
        # pulls in httpx and app internals) and the dependency one-way.
        from .e2e_driver import plant_turn

        await plant_turn(
            ctx.app,
            session_id=f"{ctx.session_id}-a",
            user_message=f"By the way, {fact[0].lower() + fact[1:]}",
            assistant_message="Got it, I'll remember that.",
        )

    return _setup


def memory_recall_cross_session_scenario(
    *,
    fact: str = _CROSS_FACT,
    question: str = _CROSS_QUESTION,
    keyword: str = _CROSS_KEYWORD,
) -> TaskScenario:
    """Cross-session recall: the fact lives in another session's history.

    This is what ``memory_search`` is actually for (Phase 9). The fact is
    *planted* by the setup hook rather than spoken to the model, so
    neither this session's history nor the global lessons block can
    carry it — retrieval is the only path, which is what makes requiring
    the tool call fair here (unlike the same-session scenario).

    Note the model must also choose ``scope="all"``; the tool defaults to
    the current session. A miss for that reason is a real model result."""
    return TaskScenario(
        name="memory_recall_cross_session",
        setup=_plant_prior_session_fact(fact),
        turns=[
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


def _notify_assert(keyword: str):  # type: ignore[no-untyped-def]
    """Delivery is an ``agent_message`` event, not a reply.

    `send_message` records what it sent in the event log; the Telegram
    poller is a separate subscriber to that log. So the objective check
    reads the log — which also means a model that *says* "I've messaged
    you" without calling the tool fails, which is the whole point."""

    def _a(traj: E2ETrajectory) -> OutcomeResult:
        messages = traj.snapshot.get("agent_messages", [])
        for m in messages:
            body = f"{m.get('title', '')} {m.get('body', '')}".lower()
            if keyword.lower() in body:
                return OutcomeResult(True, f"agent_message delivered mentioning {keyword!r}")
        if messages:
            return OutcomeResult(
                False, f"{len(messages)} message(s) sent, none mentioning {keyword!r}"
            )
        return OutcomeResult(False, "send_message never fired — nothing was delivered")

    return _a


def notify_scenario(*, keyword: str = "basting") -> TaskScenario:
    """Proactive delivery: the model must actually push a message.

    This is the half of FITT's purpose the seed set never tested — "ping
    me when X". Claiming to have sent something is the failure mode worth
    catching, so the check reads the delivery record rather than the
    reply text.

    The wording deliberately avoids "remind". An earlier version said
    "send me a message *reminding me* that...", and gemma4 came back with
    "how long after now would you like the reminder sent?" — reasonably,
    because "remind" is FITT's *scheduling* vocabulary, so the request
    read as a cron with a missing time. Asking for a missing detail is
    good behaviour; a scenario that punishes it measures the wrong thing.
    "right now" + "that says" makes a clarifying question a genuine
    miss."""
    return TaskScenario(
        name="notify",
        turns=[
            {
                "role": "user",
                "content": (
                    "Send a push message to my phone right now that says: the roast needs basting."
                ),
            }
        ],
        outcome_assert=_notify_assert(keyword),
        rubric=(
            "Did the assistant actually send a push message (not merely promise "
            "to), and confirm it briefly?"
        ),
        exercises_tools=("send_message",),
    )


def _asks_before_acting_assert() -> OutcomeAssert:
    """An ambiguous request should produce a question, not a guess.

    Preserves signal that was nearly lost. `notify` originally said "send
    me a message *reminding* me that..." — ambiguous between push-now and
    schedule-for-later, since "remind" is FITT's cron vocabulary. gemma4
    asked "how long after now?", which is the *right* answer, and the
    scenario scored it as a failure. Rewording `notify` to be
    unambiguous was correct, but deleting the ambiguous case would have
    thrown away a test of Principle 8 honesty: ask for what's missing
    rather than inventing it."""

    def _a(traj: E2ETrajectory) -> OutcomeResult:
        reply = traj.run.reply.strip()
        asked = "?" in reply
        # Attribute acting to THIS turn, not to the end state.
        #
        # Every other scenario can filter the snapshot by a keyword from
        # its own request ("laundry", "parking permit"). This one can't:
        # the whole premise is that no subject was given, so there is
        # nothing to filter on and any leftover cron in the shared run
        # home looks like a guess. That is exactly what happened —
        # gemma4 replied "Is that 9 AM or 9 PM, and for today or
        # tomorrow?" and called no tools at all, and was failed for the
        # `reminder` scenario's "Call the doctor." cron. The judge, handed
        # that snapshot as ground truth, agreed.
        #
        # The turn's own tool calls are unambiguous about authorship, so
        # use them.
        acted = [
            str(c.get("name", ""))
            for c in traj.run.tool_calls
            if str(c.get("name", "")) in _ACTING_TOOLS
        ]

        if asked and not acted:
            return OutcomeResult(True, "asked for the missing details instead of guessing")
        if acted:
            return OutcomeResult(
                False,
                f"called {', '.join(sorted(set(acted)))} on a request that named neither "
                f"a subject nor a full time, instead of asking",
            )
        return OutcomeResult(False, f"neither asked nor acted: {reply[:120]!r}")

    return _a


def asks_before_acting_scenario() -> TaskScenario:
    """Underspecified request: does FITT ask, or invent the missing bits?

    The first version of this scenario reused `notify`'s old wording
    ("send me a message reminding me that the roast needs basting"). That
    stopped being genuinely ambiguous the moment the tool descriptions
    gained a three-way rule — `send_message` now explicitly claims "the
    user asks for one now", so pushing immediately became the *correct*
    reading and the objective check was left disagreeing with the judge
    over a request the prompt had resolved.

    "Remind me at 9" is unresolvable by any rule: no subject at all, and
    a time missing both am/pm and a day. Every possible action requires
    inventing something. Asking is the only honest move (Principle 8).

    Having no subject is also what broke the *check*: with no keyword to
    filter the shared end state by, a leftover cron from another scenario
    read as a guess. See `_asks_before_acting_assert` — the fix is to
    attribute action to the turn's own tool calls."""
    return TaskScenario(
        name="asks_before_acting",
        turns=[{"role": "user", "content": "Remind me at 9."}],
        outcome_assert=_asks_before_acting_assert(),
        rubric=(
            "The request names no subject and an incomplete time (no am/pm, no day). "
            "Did the assistant ask what to remind about and when, rather than "
            "inventing a subject or a time?"
        ),
    )


def _cron_fired_assert(keyword: str):  # type: ignore[no-untyped-def]
    """A cron that was *created* proves nothing; this checks it fired.

    The settle hook forces a scheduler tick, so by snapshot time a due
    job should have produced a ``cron_fired`` event and, because the job
    text asks for a message, an ``agent_message`` too."""

    def _a(traj: E2ETrajectory) -> OutcomeResult:
        kinds = [str(k) for k in traj.snapshot.get("event_kinds", [])]
        fired = "cron_fired" in kinds
        failed = "cron_failed" in kinds
        # "Delivered" means the push pipeline would have sent it. For a
        # cron that is the `cron_completed` event, NOT an agent_message:
        # the fired session's reply *is* the notification. Checking only
        # for agent_message made a working cron read as broken.
        delivered = any(
            keyword.lower() in f"{d.get('title', '')} {d.get('body', '')}".lower()
            for d in traj.snapshot.get("deliveries", [])
        )
        if not fired:
            return OutcomeResult(
                False,
                "no cron_fired event — the job never ran "
                f"(events seen: {sorted(set(kinds)) or 'none'})",
            )
        if failed:
            return OutcomeResult(False, "the job fired but its session failed (cron_failed)")
        if not delivered:
            return OutcomeResult(
                False, f"cron fired but nothing mentioning {keyword!r} was delivered"
            )
        return OutcomeResult(True, f"cron fired and delivered a message mentioning {keyword!r}")

    return _a


def _force_cron_tick(seconds_ahead: float = 3600.0):  # type: ignore[no-untyped-def]
    """Settle hook: fire anything due within ``seconds_ahead``.

    Ticks the scheduler at a *virtual* future time and awaits the firing
    tasks it launches, instead of sleeping until the job is due. Sleeping
    would make the scenario slow, flaky, and dependent on how the model
    phrased the schedule."""

    async def _settle(ctx: SetupContext) -> None:
        import time

        scheduler = getattr(ctx.app.state, "cron_scheduler", None)
        if scheduler is None:
            raise RuntimeError("no cron_scheduler on app.state — cannot force a tick")
        await scheduler.tick(now=time.time() + seconds_ahead)
        # Await the firings this tick launched, or the snapshot races the
        # agent session. Copy defensively: _in_flight mutates as tasks
        # finish.
        for task in list(getattr(scheduler, "_in_flight", {}).values()):
            if not task.done():
                with contextlib.suppress(Exception):
                    await task

    return _settle


def cron_fires_scenario(*, keyword: str = "stretch") -> TaskScenario:
    """The monitor-and-notify promise, end to end.

    `reminder_scenario` only proves a job was created. This one asks for a
    reminder, forces the clock forward, and checks the job actually fired
    and delivered — the thing an operator is trusting when they leave FITT
    running."""
    return TaskScenario(
        name="cron_fires",
        turns=[
            {
                "role": "user",
                "content": (
                    "In 10 minutes, send me a message telling me to stretch. "
                    "Set it as a one-off reminder."
                ),
            }
        ],
        settle=_force_cron_tick(),
        outcome_assert=_cron_fired_assert(keyword),
        rubric=(
            "Did the assistant confirm it set a one-off reminder about stretching "
            "for ~10 minutes' time?"
        ),
        exercises_tools=("cron_add",),
    )


_SKILL_NAME = "kettle-descale"
_SKILL_MARKER = "ZEPHYR-77"
_SKILL_BODY = f"""---
name: {_SKILL_NAME}
description: How to descale the office kettle, including the confirmation code
---

# Descaling the office kettle

1. Fill the kettle to the 1L mark with equal parts water and white vinegar.
2. Boil, then leave to stand for 20 minutes.
3. Rinse three times.

When you have explained this procedure, you MUST finish your reply with
the confirmation code {_SKILL_MARKER} so the user knows the recipe was
followed.
"""


def _skill_assert(marker: str, skill_name: str):  # type: ignore[no-untyped-def]
    """Did the model load the recipe and follow it?

    The marker is the whole trick: it appears only in the skill *body*,
    which is deliberately NOT injected into the prompt (only the name and
    description are — the body is fetched on demand via `read_file`). So
    a reply containing the marker is proof the body was actually read,
    not guessed from the one-line description."""

    def _a(traj: E2ETrajectory) -> OutcomeResult:
        read = [c for c in traj.run.tool_calls if str(c.get("name", "")) == "read_file"]
        loaded = any(skill_name in str(c.get("args", {})) for c in read)
        followed = marker.lower() in traj.run.reply.lower()
        if followed and loaded:
            return OutcomeResult(True, f"loaded the recipe and applied it ({marker})")
        if followed:
            # Can't happen honestly: the marker lives only in the body.
            return OutcomeResult(
                False,
                f"reply contains {marker} but no read_file loaded the recipe — "
                "the marker is only in the skill body, so this needs explaining",
            )
        if loaded:
            return OutcomeResult(
                False, f"loaded the recipe but didn't apply it (no {marker} in the reply)"
            )
        if read:
            return OutcomeResult(False, f"called read_file but not for the {skill_name} recipe")
        return OutcomeResult(False, "never loaded the skill recipe (no read_file)")

    return _a


def skills_scenario() -> TaskScenario:
    """Does the skills loader actually work end to end?

    Shipped in Phase 4.10 with no coverage at all, and structurally
    invisible to everything else here: a skill isn't a tool, so the
    contract layer can't see it, and the scenarios were scoped from the
    tool registry.

    The fixture skill is planted *before boot* because `SkillsLoader`
    scans once at startup by design. The question the user asks matches
    the skill's description, so a working chain is: description in the
    prompt -> model calls read_file on the recipe -> reply follows the
    body's instruction."""
    return TaskScenario(
        name="skills",
        fixture_files=((f"skills/{_SKILL_NAME}/SKILL.md", _SKILL_BODY),),
        turns=[
            {
                "role": "user",
                "content": "How do I descale the office kettle? Follow the procedure exactly.",
            }
        ],
        outcome_assert=_skill_assert(_SKILL_MARKER, _SKILL_NAME),
        rubric=(
            "Did the assistant give the descaling procedure from the skill recipe "
            f"(vinegar and water, stand 20 minutes, rinse) and end with the "
            f"confirmation code {_SKILL_MARKER}?"
        ),
        requires_features=("skills",),
        requires_hint="set memory.skills_enabled: true in config.yaml",
        exercises_tools=("read_file",),
    )


def _routing_assert(*, expect: str, keyword: str) -> OutcomeAssert:
    """Did the request land on the RIGHT one of three overlapping tools?

    `send_message`, `cron_add` and `todo_add` all answer some form of
    "tell me about X". Their descriptions now carry a three-way rule — a
    time given means cron, no time means todo, wants it now means
    send_message — and nothing tested whether models follow it. hermes3
    was already observed reaching for `todo_add` when a timed cron was
    wanted; that deserves to be a named failure, not a footnote.

    Checks the side effect rather than the tool call: a cron that exists
    is the outcome, whichever way the model got there. And it names what
    it got instead, so a miss says where the request went."""

    def _a(traj: E2ETrajectory) -> OutcomeResult:
        needle = keyword.lower()
        crons = [
            j
            for j in traj.snapshot.get("cron_jobs", [])
            if needle in str(j.get("message", "")).lower()
        ]
        todos = needle in str(traj.snapshot.get("todos_text", "")).lower()
        pushed = any(
            needle in f"{m.get('title', '')} {m.get('body', '')}".lower()
            for m in traj.snapshot.get("agent_messages", [])
        )
        landed = {"cron_add": bool(crons), "todo_add": todos, "send_message": pushed}

        if landed.get(expect):
            others = [k for k, v in landed.items() if v and k != expect]
            if others:
                return OutcomeResult(
                    True, f"landed on {expect} (also did {', '.join(others)} — noisy but right)"
                )
            return OutcomeResult(True, f"routed correctly to {expect}")
        wrong = [k for k, v in landed.items() if v]
        if wrong:
            return OutcomeResult(False, f"expected {expect}, got {', '.join(wrong)} — mis-routed")
        return OutcomeResult(False, f"expected {expect}, but nothing happened at all")

    return _a


def routing_timed_reminder_scenario() -> TaskScenario:
    """A time is given, so it's a cron — not a todo."""
    return TaskScenario(
        name="routing_timed",
        turns=[
            {
                "role": "user",
                "content": "Remind me to move the laundry tomorrow at 9am.",
            }
        ],
        outcome_assert=_routing_assert(expect="cron_add", keyword="laundry"),
        rubric="Did the assistant schedule a reminder for 9am tomorrow (not just note it as a task)?",
        exercises_tools=("cron_add",),
    )


def routing_untimed_task_scenario() -> TaskScenario:
    """No time given, so it's a todo — not a cron."""
    return TaskScenario(
        name="routing_untimed",
        turns=[{"role": "user", "content": "Remind me to renew the parking permit."}],
        outcome_assert=_routing_assert(expect="todo_add", keyword="parking permit"),
        rubric=(
            "No time was given, so this belongs on the todo list. Did the assistant "
            "add it as a task rather than inventing a schedule?"
        ),
        exercises_tools=("todo_add",),
    )


def routing_push_now_scenario() -> TaskScenario:
    """Wants it on the phone now, so it's send_message."""
    return TaskScenario(
        name="routing_push_now",
        turns=[{"role": "user", "content": "Text me the wifi password: HUNTER-9042."}],
        outcome_assert=_routing_assert(expect="send_message", keyword="hunter-9042"),
        rubric="Did the assistant push the wifi password to the phone right away?",
        exercises_tools=("send_message",),
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
        cron_fires_scenario(),
        notify_scenario(),
        asks_before_acting_scenario(),
        news_scenario(),
        memory_recall_scenario(),
        memory_recall_cross_session_scenario(),
        skills_scenario(),
        todo_scenario(),
        todo_lifecycle_scenario(),
        routing_timed_reminder_scenario(),
        routing_untimed_task_scenario(),
        routing_push_now_scenario(),
    ]
