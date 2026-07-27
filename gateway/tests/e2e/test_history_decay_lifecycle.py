"""Phase 5 — decaying-history end-to-end lifecycle (retires manual 13e).

`test_memory_decay.py` unit-tests the 4-layer decay rendering by calling
`_load_decaying_history(..., now=<fixed date>)` directly. This test proves
the other half the manual live-check was for: that the decayed history
actually reaches the **dispatched** prompt through the full HTTP pipeline
(auth -> memory.load_context -> body build -> router dispatch).

Seed yesterday + today under session `main`, POST a chat turn, and assert
the wire dispatch shows yesterday collapsed to its first turn + the
truncation marker while today rides verbatim. Dates are seeded relative to
`_today()` (the same UTC date the code uses at dispatch), so no clock
injection is needed and there's no midnight-boundary flake beyond the
sub-second window between seeding and the POST.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx

from gateway.memory import _today

from .._llm_stubs import stub_reply
from .conftest import StubbedLLM


def _seed_day(memory: Any, session: str, day: date, turns: list[tuple[str, str]]) -> None:
    """Write a dated history file in the on-disk turn format the parser
    expects (``## <ts> <role>`` headers, blank-line-separated)."""
    path = memory.history_path(session, day=day)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for idx, (role, content) in enumerate(turns):
        ts = f"{day.isoformat()}T10:{idx:02d}:00Z"
        lines.append(f"## {ts} {role}\n\n{content}\n")
    path.write_text("\n".join(lines), encoding="utf-8")


async def test_yesterday_history_decays_in_dispatched_prompt(
    e2e_app: Any,
    e2e_client: httpx.AsyncClient,
    stubbed_llm: StubbedLLM,
) -> None:
    today = _today()
    yesterday = today - timedelta(days=1)
    memory = e2e_app.state.memory

    # Yesterday: two user-anchored turns. The decay layer keeps the
    # first turn verbatim and collapses the rest behind a count marker.
    _seed_day(
        memory,
        "main",
        yesterday,
        [
            ("user", "YESTERDAY_FIRST_USER"),
            ("assistant", "yesterday first answer"),
            ("user", "YESTERDAY_SECOND_USER"),
            ("assistant", "yesterday second answer"),
        ],
    )
    # Today: rides verbatim (layer 3).
    _seed_day(
        memory,
        "main",
        today,
        [
            ("user", "TODAY_USER"),
            ("assistant", "TODAY_ASSISTANT"),
        ],
    )

    stubbed_llm.load([stub_reply("ok")])
    r = await e2e_client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-default",
            "messages": [{"role": "user", "content": "current question"}],
        },
    )
    assert r.status_code == 200, r.text

    # Inspect what actually went to the backend on this turn.
    dispatched = stubbed_llm.calls[-1].get("messages", [])
    blob = "\n".join(m.get("content", "") for m in dispatched if isinstance(m.get("content"), str))

    # Yesterday's first turn survived; its later turn was collapsed
    # behind the truncation marker (layer 2).
    assert "YESTERDAY_FIRST_USER" in blob, (
        "yesterday's first turn should reach the dispatched prompt; the "
        "decay layer isn't being injected through the HTTP path"
    )
    assert "YESTERDAY_SECOND_USER" not in blob, (
        "yesterday's later turns should be collapsed, not sent verbatim"
    )
    assert "[Yesterday:" in blob and "more user turn" in blob, (
        "the yesterday truncation marker should appear in the dispatch"
    )
    # Today rides verbatim (layer 3).
    assert "TODAY_USER" in blob
    assert "TODAY_ASSISTANT" in blob
