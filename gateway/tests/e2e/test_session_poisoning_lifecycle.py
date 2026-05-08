"""U1.5 — session poisoning lifecycle (Phase 5 fix pinned).

The Phase 4 failure mode: only the user message and the final
assistant reply persist to history. Tool calls and tool results
are ephemeral. A turn where the assistant's reply was "SSH is
unreachable" becomes part of history as a factual claim; future
turns read it, pattern-match on it, and refuse to call tools
even after the issue is fixed.

Phase 5 fixes this by persisting tool-using turns structurally:
``user`` → ``assistant tool_calls`` → ``tool <name>`` (with
``ok`` or ``exit=N: <brief>``) → final assistant. Loading such
a turn gives the model both the paraphrased reply AND the
structured tool result, so it can tell a stale refusal from
current reality.

This test pins that fix at the HTTP surface: seed a
tool-using-turn on disk where the tool result was ``ok`` but
the assistant's natural-language reply claims failure. Fire a
new request. Assert the structured ``ok`` outcome reaches the
model's dispatched messages so a model reading history can
override the paraphrase.

Pre-Phase-5 this test would have been impossible to write — the
format couldn't carry the tool outcome in the first place. The
test being expressible in Phase 5 shape is itself evidence the
fix landed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from .._llm_stubs import stub_reply
from .conftest import StubbedLLM


async def test_tool_outcome_reaches_model_alongside_stale_paraphrase(
    e2e_app: Any,
    e2e_client: httpx.AsyncClient,
    stubbed_llm: StubbedLLM,
) -> None:
    """Seed a poisoned tool-using turn on disk, fire a new
    request, assert the tool's ``ok`` outcome is visible in
    the dispatched messages.

    The poisoning pattern we're documenting: the assistant's
    NL reply says "SSH unreachable" but the actual tool call
    succeeded (status=ok). Before Phase 5 only the NL reply
    persisted → future turns saw only the refusal. After
    Phase 5 the structured tool result persists alongside →
    future turns see the ground truth too.
    """
    # Seed today's history with the Phase 5 tool-using shape:
    # user → assistant tool_calls → tool (ok) → final
    # assistant (stale NL refusal). Only possible because
    # Phase 5 extended the parser to recognise these blocks.
    today = datetime.now(UTC).date()
    history_dir = e2e_app.state.config.memory.sessions_dir / "main" / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    poisoned = history_dir / f"{today.isoformat()}.md"
    poisoned.write_text(
        "## 2026-05-07T10:00:00Z user\n\n"
        "fetch my repo\n\n"
        "## 2026-05-07T10:00:05Z assistant tool_calls\n\n"
        "- run_tests(project='hub')\n\n"
        "## 2026-05-07T10:00:06Z tool run_tests\n\n"
        "ok\n\n"
        "## 2026-05-07T10:00:10Z assistant\n\n"
        "SSH is unreachable, please configure keys and try again.\n",
        encoding="utf-8",
    )

    stubbed_llm.load([stub_reply("checking.")])
    r = await e2e_client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-default",
            "messages": [{"role": "user", "content": "try again please"}],
            "tool_choice": "auto",
        },
    )
    assert r.status_code == 200

    dispatch_kwargs = stubbed_llm.calls[0]
    dispatched = dispatch_kwargs.get("messages", [])

    # Phase 5 guarantee #1: the tool's structured outcome (role
    # tool with content "ok") reaches the model alongside the
    # paraphrased reply. A model receiving this can tell the
    # NL refusal is stale.
    tool_messages = [m for m in dispatched if m.get("role") == "tool"]
    assert tool_messages, (
        "no role=tool entry in dispatched messages; Phase 5's "
        "tool-outcome-persistence didn't land. The model can't "
        "see the ground truth and will keep believing the stale "
        "NL paraphrase."
    )
    assert any("ok" in (m.get("content") or "") for m in tool_messages), (
        "role=tool entry present but its content doesn't carry the 'ok' status we seeded on disk"
    )

    # Phase 5 guarantee #2: the assistant tool_calls entry is
    # also present with a matching tool_call_id linking the
    # assistant turn to the tool result. Without the pairing,
    # some LLM providers reject the message list as malformed.
    assistant_with_calls = [
        m for m in dispatched if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert assistant_with_calls, (
        "no assistant turn with tool_calls in the dispatched "
        "messages; the parser didn't reconstruct the pairing "
        "that makes the role=tool entry valid"
    )
    call_ids = {tc["id"] for m in assistant_with_calls for tc in m["tool_calls"] if "id" in tc}
    tool_ids = {m["tool_call_id"] for m in tool_messages if "tool_call_id" in m}
    assert call_ids & tool_ids, (
        "assistant tool_calls ids don't pair with any tool "
        "role entries; the LLM would reject this message list"
    )
