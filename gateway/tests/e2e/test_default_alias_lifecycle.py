"""U1.3 — default alias lifecycle.

Pins the 'models are configuration, not architecture' principle
at the lifecycle layer:

* A cron with ``agent_alias=""`` fires against the operator's
  configured ``fitt-default`` alias.
* A cron with an explicit ``agent_alias`` uses it — the
  operator's choice wins.

This catches the 2026-05-07 silent-default regression where
cron firings were routed to ``fitt-smart`` without the operator
asking for it. A unit-level pin already exists
(``test_default_alias_prefers_fitt_default``) at the
``CronRunner._default_alias`` layer; this is the end-to-end
version that pins the whole dispatch path.
"""

from __future__ import annotations

from typing import Any

import httpx

from .._llm_stubs import stub_reply
from .conftest import E2EClock, StubbedLLM


async def test_unset_agent_alias_resolves_to_fitt_default(
    e2e_app: Any,
    e2e_client: httpx.AsyncClient,
    e2e_clock: E2EClock,
    stubbed_llm: StubbedLLM,
) -> None:
    """A cron without ``agent_alias`` fires against the concrete
    model bound to ``fitt-default``. For the test config that's
    ``qwen-big`` (ollama, qwen2.5-coder:14b)."""
    from gateway.cron import CronJob, CronSchedule

    job = e2e_app.state.cron.add(
        CronJob(
            id="",
            name="default-alias probe",
            message="ping",
            schedule=CronSchedule(kind="every", every_secs=60),
            created_ts=e2e_clock.now,
        )
    )
    assert job.agent_alias == ""

    stubbed_llm.load([stub_reply("pong")])

    e2e_clock.advance(120.0)
    await e2e_clock.run_due(e2e_app.state.cron_scheduler)

    # The dispatched model kwarg should be the one bound to
    # fitt-default — NOT fitt-smart. The LiteLLM shape is
    # "<provider>/<model>" so we match the suffix.
    assert len(stubbed_llm.calls) == 1, (
        f"expected exactly one dispatch for the firing; got {len(stubbed_llm.calls)}"
    )
    dispatched_model = stubbed_llm.calls[0].get("model")
    assert dispatched_model == "ollama_chat/qwen2.5-coder:14b", (
        f"expected dispatch against fitt-default's binding "
        f"(ollama_chat/qwen2.5-coder:14b), got {dispatched_model!r}. "
        "A 'fitt-smart' dispatch here would mean the cron runner "
        "silently upgraded the operator's default alias."
    )


async def test_explicit_agent_alias_wins(
    e2e_app: Any,
    e2e_client: httpx.AsyncClient,
    e2e_clock: E2EClock,
    stubbed_llm: StubbedLLM,
) -> None:
    """Counter-case: when the operator sets ``agent_alias`` on a
    cron, that choice IS respected. Same principle — models are
    configuration — applied in reverse: we must not second-guess
    the explicit setting either."""
    from gateway.cron import CronJob, CronSchedule

    e2e_app.state.cron.add(
        CronJob(
            id="",
            name="smart-alias probe",
            message="think hard",
            schedule=CronSchedule(kind="every", every_secs=60),
            created_ts=e2e_clock.now,
            agent_alias="fitt-smart",
        )
    )

    stubbed_llm.load([stub_reply("pondered")])

    e2e_clock.advance(120.0)
    await e2e_clock.run_due(e2e_app.state.cron_scheduler)

    assert len(stubbed_llm.calls) == 1
    dispatched_model = stubbed_llm.calls[0].get("model")
    assert dispatched_model == "openrouter/anthropic/claude-sonnet-4.5", (
        f"agent_alias='fitt-smart' should route to the sonnet binding, got {dispatched_model!r}"
    )
