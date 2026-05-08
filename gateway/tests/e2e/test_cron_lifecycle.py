"""U1.1 — cron happy-path lifecycle.

Drives the full pipeline:
1. User asks via HTTP → stubbed LLM emits ``cron_add`` tool call.
2. Approver approves (scripted).
3. Chat returns; cron persists.
4. Clock advances past the first firing; scheduler ticks.
5. Stubbed LLM answers the firing with a natural-language reply.
6. ``/v1/events`` has exactly one ``cron_fired`` and one
   ``cron_completed``. No duplicate ``cron_fired`` push.

That last assertion pins the 2026-05-07 bug where ``cron_fired``
was surfaced to Telegram alongside ``cron_completed``, producing
two notifications per firing.
"""

from __future__ import annotations

from typing import Any

import httpx

from .._llm_stubs import stub_reply, stub_tool_call
from .conftest import (
    E2EApprover,
    E2EClock,
    StubbedLLM,
    fetch_events,
)


async def test_cron_tool_call_creates_and_fires(
    e2e_app: Any,
    e2e_client: httpx.AsyncClient,
    e2e_approver: E2EApprover,
    e2e_clock: E2EClock,
    stubbed_llm: StubbedLLM,
) -> None:
    """Full lifecycle: tool call → approval → fire → events."""
    # Trajectory:
    # - Round 1: user asks → LLM emits cron_add
    # - Round 2: after tool returns ok, LLM wraps up with a reply
    # - Round 3: cron fires and LLM answers the firing
    stubbed_llm.load(
        [
            stub_tool_call(
                "cron_add",
                {
                    "name": "briefing",
                    "message": "summarise my open PRs",
                    "schedule_spec": "every 60s",
                },
            ),
            stub_reply("Created a briefing cron for you."),
            stub_reply("Here is your briefing. Nothing urgent."),
        ]
    )

    # Scripted approver: approve anything that comes in, which
    # for this trajectory is only the cron_add.
    async with e2e_approver.start(lambda p: "approve"):
        r = await e2e_client.post(
            "/v1/chat/completions",
            json={
                "model": "fitt-default",
                "messages": [{"role": "user", "content": "set up a briefing every minute"}],
                "tool_choice": "auto",
            },
        )
    assert r.status_code == 200, r.text
    reply = r.json()["choices"][0]["message"]["content"]
    assert "briefing" in reply.lower()

    # Exactly one cron should exist now.
    crons = e2e_app.state.cron.list(include_disabled=True)
    assert len(crons) == 1
    job = crons[0]
    assert job.name == "briefing"

    # Baseline event count — everything from here is from the
    # firing we're about to trigger.
    since = 0.0
    prior_events = await fetch_events(e2e_client, since=since, limit=1000)
    prior_fired = [e for e in prior_events if e["kind"] == "cron_fired"]
    prior_completed = [e for e in prior_events if e["kind"] == "cron_completed"]

    # The cron's next-firing window is 60s out. Advance past it,
    # then drive one scheduler tick.
    e2e_clock.advance(120.0)
    fired_ids = await e2e_clock.run_due(e2e_app.state.cron_scheduler)
    assert fired_ids == [job.id]

    # After the firing, exactly one new cron_fired and one new
    # cron_completed should have landed. The 2026-05-07 regression
    # we're pinning: cron_fired was double-delivered.
    after_events = await fetch_events(e2e_client, limit=1000)
    after_fired = [e for e in after_events if e["kind"] == "cron_fired"]
    after_completed = [e for e in after_events if e["kind"] == "cron_completed"]

    assert len(after_fired) == len(prior_fired) + 1, (
        "expected exactly one new cron_fired event after one firing; "
        f"got {len(after_fired) - len(prior_fired)} new events"
    )
    assert len(after_completed) == len(prior_completed) + 1, (
        "expected exactly one new cron_completed event after one firing"
    )

    # The completed event carries the LLM's firing-time reply.
    fired_completed = after_completed[-1]
    assert "briefing" in fired_completed["body"].lower()
    assert fired_completed["meta"]["cron_id"] == job.id

    # And no duplicate cron_completed for the same firing.
    matching = [e for e in after_completed if e.get("meta", {}).get("cron_id") == job.id]
    assert len(matching) == 1, (
        "exactly one cron_completed per firing; duplicates would "
        "cause double-notification in Telegram"
    )

    # Stubs exhausted — no runaway dispatch.
    assert stubbed_llm.remaining() == 0


async def test_second_fire_produces_second_pair(
    e2e_app: Any,
    e2e_client: httpx.AsyncClient,
    e2e_clock: E2EClock,
    stubbed_llm: StubbedLLM,
) -> None:
    """Two ticks → two firings → two cron_fired + two cron_completed.

    Pins that advancing the clock twice doesn't re-fire the same
    cron twice in one tick (dedup bug fodder), and that repeated
    firings still produce exactly one pair each.
    """
    # Register a cron directly (no tool-call round trip; we're
    # testing the scheduler, not the cron_add path).
    from gateway.cron import CronJob, CronSchedule

    job = e2e_app.state.cron.add(
        CronJob(
            id="",
            name="heartbeat",
            message="ping",
            schedule=CronSchedule(kind="every", every_secs=60),
            created_ts=e2e_clock.now,
        )
    )

    # Two rounds of LLM replies (one per firing).
    stubbed_llm.load([stub_reply("pong 1"), stub_reply("pong 2")])

    # First firing window.
    e2e_clock.advance(120.0)
    fired1 = await e2e_clock.run_due(e2e_app.state.cron_scheduler)
    assert fired1 == [job.id]

    # Second firing window (another 60s past last_run_ts).
    e2e_clock.advance(120.0)
    fired2 = await e2e_clock.run_due(e2e_app.state.cron_scheduler)
    assert fired2 == [job.id]

    events = await fetch_events(e2e_client, limit=1000)
    completed = [
        e
        for e in events
        if e["kind"] == "cron_completed" and e.get("meta", {}).get("cron_id") == job.id
    ]
    fired = [
        e
        for e in events
        if e["kind"] == "cron_fired" and e.get("meta", {}).get("cron_id") == job.id
    ]
    assert len(fired) == 2
    assert len(completed) == 2
    bodies = [e["body"] for e in completed]
    assert bodies == ["pong 1", "pong 2"]
