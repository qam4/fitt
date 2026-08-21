"""``ToolResult.user_note`` — facts the user sees whatever the model says.

Added 2026-08-20. Cron schedule confirmation used to be advisory: the tool
result stated the resolved local fire time and asked the model to relay it.
A judged run caught the model replying "scheduled that reminder for you in
10 minutes" — the relative phrasing the confirmation exists to eliminate —
in roughly one sample in three. The objective assert couldn't see it (the
cron was created and fired), so only the judge noticed.

These test the append, which is the fix. The cron tool's half is pinned in
test_tools_cron.py.
"""

from __future__ import annotations

from gateway.agent_loop import _append_user_notes


def test_a_note_is_appended_when_the_model_did_not_say_it() -> None:
    """The reported failure, in miniature."""
    out = _append_user_notes(
        "OK. I've scheduled that reminder for you in 10 minutes.",
        [("This fires Thu 20 Aug at 10:18 (EDT).", "10:18")],
    )

    assert "10:18" in out
    assert out.startswith("OK. I've scheduled")


def test_a_note_is_suppressed_when_the_model_already_said_it() -> None:
    """A model that did its job must not be followed by a redundant
    restatement of the same time."""
    reply = "Scheduled — it fires today at 10:18 (EDT)."

    assert _append_user_notes(reply, [("This fires Thu 20 Aug at 10:18 (EDT).", "10:18")]) == reply


def test_the_same_note_twice_appears_once() -> None:
    """Two cron_add calls in one turn shouldn't produce the sentence
    twice."""
    note = ("This fires Thu 20 Aug at 10:18 (EDT).", "10:18")

    out = _append_user_notes("Done.", [note, note])

    assert out.count("This fires") == 1


def test_two_different_notes_both_survive() -> None:
    out = _append_user_notes(
        "Done.",
        [
            ("This fires Thu 20 Aug at 10:18 (EDT).", "10:18"),
            ("This fires Fri 21 Aug at 09:00 (EDT).", "09:00"),
        ],
    )

    assert "10:18" in out and "09:00" in out


def test_no_notes_leaves_the_reply_untouched() -> None:
    """Most turns have no notes; they must be byte-identical."""
    assert _append_user_notes("just a reply", []) == "just a reply"


def test_an_empty_probe_always_appends() -> None:
    """The probe is opt-in dedupe, not a requirement."""
    out = _append_user_notes("reply", [("Something important.", "")])

    assert "Something important." in out


def test_a_note_survives_an_empty_reply() -> None:
    """A model that returned nothing is exactly when the user most needs
    the fact — and naive concatenation would leave leading blank lines."""
    out = _append_user_notes("", [("This fires Thu 20 Aug at 10:18 (EDT).", "10:18")])

    assert out == "This fires Thu 20 Aug at 10:18 (EDT)."


# --------------------------------------------------------------- through the loop
#
# The unit tests above would all pass with the accumulator never wired
# into run_agent_loop. This is the one that fails if it isn't.


async def test_a_tools_user_note_reaches_the_delivered_reply(
    monkeypatch: object,
) -> None:
    from typing import Any

    import pytest

    from gateway.agent_loop import run_agent_loop
    from gateway.config import Config
    from gateway.router import AliasRouter
    from gateway.tools import (
        ApprovalBucket,
        ApprovalDecision,
        Tool,
        ToolContext,
        ToolRegistry,
        ToolResult,
    )

    from ._fixtures import build_test_config
    from ._llm_stubs import make_response, make_tool_call

    assert isinstance(monkeypatch, pytest.MonkeyPatch)

    async def _impl(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult.ok(
            "created cron 'x'",
            user_note="This fires Thu 20 Aug at 10:18 (EDT).",
            user_note_probe="10:18",
        )

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="pretend_cron_add",
            description="stands in for cron_add",
            schema={"type": "object", "properties": {}},
            callable=_impl,
            default_bucket=ApprovalBucket.AUTO,
        )
    )

    class _AlwaysAuto:
        async def check(self, tool: Any, args: Any, context: Any) -> Any:
            return ApprovalDecision.auto()

    calls = iter(
        [
            make_response(tool_calls=[make_tool_call("c1", "pretend_cron_add", {})]),
            # The observed failure: a relative phrase and no absolute time.
            make_response(content="OK. I've scheduled that reminder in 10 minutes."),
        ]
    )

    async def fake(**kwargs: Any) -> Any:
        return next(calls)

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    import tempfile
    from pathlib import Path

    cfg: Config = build_test_config(Path(tempfile.mkdtemp()))
    result = await run_agent_loop(
        alias="fitt-default",
        messages=[{"role": "user", "content": "remind me in 10 minutes"}],
        request_body_extras={"tools": [], "tool_choice": "auto"},
        alias_router=AliasRouter(cfg),
        tool_registry=registry,
        approval=_AlwaysAuto(),
        tool_ctx=ToolContext(
            client="cli",
            session_key="s1",
            projects=None,
            policy=registry.policy,
        ),
        session_key="s1",
    )

    assert result.status == "ok", result.status
    assert "10:18" in result.assistant_text, (
        "the tool's user_note never reached the reply — the accumulator is "
        f"not wired into run_agent_loop. got: {result.assistant_text!r}"
    )
