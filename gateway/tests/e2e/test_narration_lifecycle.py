"""U1.2 — narration lifecycle.

The 2026-05-07 failure mode: weak models write a JSON-fenced
tool call in ``content`` instead of emitting the structured
``tool_calls`` array. The agent loop treats the reply as a
natural stop (no calls to execute) and the narration ends up
as the user-facing body.

This pins the observability contract across the full pipeline:
a narrated tool call at fire time still produces ``cron_completed``
(the run completes — the body just happens to be unhelpful) AND
a ``tool_call_narrated`` event so the operator sees it in
``fitt inbox``. ``test_cron_runner.py`` has a unit-level version;
this is the insurance that a future refactor can't break the
end-to-end wiring.
"""

from __future__ import annotations

from typing import Any

import httpx

from .._llm_stubs import stub_narrated_tool_call
from .conftest import E2EClock, StubbedLLM, fetch_events


async def test_narrated_firing_emits_both_events(
    e2e_app: Any,
    e2e_client: httpx.AsyncClient,
    e2e_clock: E2EClock,
    stubbed_llm: StubbedLLM,
) -> None:
    """Seed a cron directly, fire it with a narrated reply,
    assert both events land."""
    from gateway.cron import CronJob, CronSchedule

    job = e2e_app.state.cron.add(
        CronJob(
            id="",
            name="narration canary",
            message="just remind me",
            schedule=CronSchedule(kind="every", every_secs=60),
            created_ts=e2e_clock.now,
        )
    )

    stubbed_llm.load(
        [
            stub_narrated_tool_call(
                "send_message",
                {"text": "reminder"},
                preamble="I will call send_message now.",
            )
        ]
    )

    e2e_clock.advance(120.0)
    fired = await e2e_clock.run_due(e2e_app.state.cron_scheduler)
    assert fired == [job.id]

    events = await fetch_events(e2e_client, limit=1000)

    # cron_completed still lands — the narration doesn't
    # invalidate the firing.
    completed = [
        e
        for e in events
        if e["kind"] == "cron_completed" and e.get("meta", {}).get("cron_id") == job.id
    ]
    assert len(completed) == 1, (
        "the narration path is still a 'completed' firing — the "
        "body is garbage but the run finished"
    )
    # Body contains the narration (the raw reply from the LLM).
    assert "send_message" in completed[0]["body"]

    # tool_call_narrated is the operator-facing signal.
    narrated = [
        e
        for e in events
        if e["kind"] == "tool_call_narrated"
        and e.get("session_key", "").startswith(f"cron:{job.id}:")
    ]
    assert len(narrated) == 1, (
        "expected exactly one tool_call_narrated event from the "
        "narrated firing; without it the operator has no signal "
        "that the model is misbehaving"
    )
    assert narrated[0]["meta"]["tool_name"] == "send_message"


async def test_clean_firing_emits_no_narration_event(
    e2e_app: Any,
    e2e_client: httpx.AsyncClient,
    e2e_clock: E2EClock,
    stubbed_llm: StubbedLLM,
) -> None:
    """Counter-case: a plain natural-language reply must NOT
    trip the narration detector. Without this, the detector
    could pathologically match on any JSON-adjacent prose."""
    from gateway.cron import CronJob, CronSchedule

    from .._llm_stubs import stub_reply

    job = e2e_app.state.cron.add(
        CronJob(
            id="",
            name="clean reply",
            message="say hi",
            schedule=CronSchedule(kind="every", every_secs=60),
            created_ts=e2e_clock.now,
        )
    )

    stubbed_llm.load([stub_reply("Done. Test works!")])

    e2e_clock.advance(120.0)
    await e2e_clock.run_due(e2e_app.state.cron_scheduler)

    events = await fetch_events(e2e_client, limit=1000)
    narrated = [
        e
        for e in events
        if e["kind"] == "tool_call_narrated"
        and e.get("session_key", "").startswith(f"cron:{job.id}:")
    ]
    assert narrated == [], (
        "a clean reply should not produce a tool_call_narrated event; got {narrated!r}"
    )
