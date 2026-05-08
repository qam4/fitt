"""U1.5 — session poisoning lifecycle.

Documents the known Phase 4 limitation that Phase 5 will fix:
only the user message and the final assistant reply persist to
history today. Tool calls and tool results are ephemeral. A
turn where the assistant's reply was "SSH is unreachable"
becomes part of history as a factual claim; future turns read
it, pattern-match on it, and refuse to use tools even after the
issue is fixed.

The test seeds a session's history with the poisoning pattern,
fires a new request, and asserts the stale reply reaches the
LLM dispatch. That's the invariant Phase 5 will break by
persisting tool-call turns structurally (user → tool_calls →
tool_result → assistant), so this test flips green when Phase 5
lands.

``@pytest.mark.xfail(strict=True)`` means:
* Today, the test body would fail (Phase 4's memory serialises
  the stale reply verbatim, and the LLM doesn't produce
  duplicate tool calls from a stub that's just ``stub_reply``).
* Actually — let's think about the exact shape. What we want
  to pin is: **given a history with a stale "I can't do X"
  reply, do we pass that stale content to the LLM?** Today:
  YES. Phase 5's fix: replace the stale NL reply with a
  structured tool-call record, so the model sees the outcome
  (``ok`` or a short error) not the narrative refusal.

So the concrete assertion this test pins is: the stale
assistant text IS visible in the next dispatch's messages.
Phase 5 will make it NOT visible (replaced by the structured
record). Hence ``xfail(strict=True, reason=...)`` — when the
phase 5 code lands, this test starts failing (because the
stale text no longer reaches the dispatch), we flip it to a
proper positive assertion, and the suite flags the transition.

Wait — that's backwards. If the test body asserts "the stale
reply IS present" then today it PASSES (and shouldn't be
xfail). The xfail is meant to flip green on Phase 5.

The clearer framing: assert the FIXED behaviour. "The stale
assistant text is NOT verbatim in the next dispatch." Today
this fails (memory does pass it through). Phase 5's structured
persistence means the stale text gets replaced; the test
passes.

That's what xfail(strict=True) pins: failing today, passing
when the Phase 5 fix lands, and a strict-xfail flag means an
unexpected pass blows the suite so we notice the fix.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from .._llm_stubs import stub_reply
from .conftest import StubbedLLM


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Phase 5 will persist tool-call turns structurally so a "
        "stale 'SSH unreachable' reply is replaced by the "
        "tool-result outcome in context. Until then, memory "
        "serialises the stale NL reply verbatim and passes it "
        "to the next dispatch. When this test starts passing, "
        "Phase 5's memory fix has landed — flip the xfail off "
        "and keep the positive assertion."
    ),
)
async def test_stale_refusal_does_not_leak_into_next_dispatch(
    e2e_app: Any,
    e2e_client: httpx.AsyncClient,
    stubbed_llm: StubbedLLM,
) -> None:
    """Seed a poisoned history, issue a new turn, assert the
    stale refusal is NOT in the system/messages payload.

    Phase 4 memory serialises the final assistant reply
    verbatim, so the string WILL be present today — test
    fails. Phase 5 replaces it with a tool-outcome record —
    test passes.
    """
    # Seed the session's history file directly. Use today's
    # filename so MemoryStore picks it up on the next request.
    today = datetime.now(UTC).date()
    history_dir = e2e_app.state.config.memory.sessions_dir / "main" / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    poisoned = history_dir / f"{today.isoformat()}.md"
    poisoned.write_text(
        "## 2026-05-07T10:00:00Z user\n\n"
        "fetch my repo\n\n"
        "## 2026-05-07T10:00:05Z assistant\n\n"
        "SSH is unreachable, please configure keys and try again.\n",
        encoding="utf-8",
    )

    # Fire a plain follow-up. The detector we care about is on
    # the dispatch kwargs — does the stale assistant text show
    # up in the messages/content payload?
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

    # The failure we're documenting: the stale string IS present
    # in the dispatched messages today. Assert the Phase-5-fixed
    # behaviour (NOT present) so the test flips green when the
    # fix lands.
    dispatch_kwargs = stubbed_llm.calls[0]
    rendered = _render_messages(dispatch_kwargs.get("messages", []))
    assert "SSH is unreachable" not in rendered, (
        "the stale refusal from a prior turn still reaches the "
        "model — the 2026-05-07 poisoning pattern. Phase 5's "
        "structured tool-call persistence will fix this by "
        "replacing the NL refusal with the tool's 'ok' result "
        "(or a short error summary) so future context carries "
        "the outcome, not the narrative. When this assertion "
        "starts passing, move the test out of xfail."
    )


def _render_messages(messages: list[dict[str, Any]]) -> str:
    """Flatten dispatched messages into a single searchable string.

    Cheap approach: just concatenate every ``content`` field we
    find. System messages + history turns + the current user
    turn all land here. If Phase 5 replaces the stale reply
    with a structured record (tool_calls / tool_result), the
    stale NL string stops appearing.
    """
    parts: list[str] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            # OpenAI content parts shape — future-proofing.
            for part in content:
                if isinstance(part, dict) and "text" in part:
                    parts.append(str(part["text"]))
    return "\n".join(parts)
