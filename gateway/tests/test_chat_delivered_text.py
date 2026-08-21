"""The delivered HTTP body must carry the text FITT treated as the reply.

Found 2026-08-21 by reading a live eval report: a cron_add result said
"This fires Fri 21 Aug at 11:58" and the delivered reply said "in 10
minutes" with no note appended. Both the template-token strip and the
appended user_note were applied on the way to ``assistant_text`` — which
feeds memory, the logs, the gap parser and the *streaming* envelope — while
the non-streaming body kept the model's raw output.

So two fixes that looked shipped were only ever live for streaming clients.
Worse, the judged harness posts non-streaming, meaning the instrument used
to verify them was reading the uncorrected text.

These tests go through the real HTTP endpoint, because that is the only
level at which the bug is visible: every unit test of the extractor and the
note append passed the whole time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.tools import ApprovalBucket, Tool, ToolContext, ToolResult

from ._fixtures import PERSONAL_TOKEN, build_test_config
from ._llm_stubs import make_response, make_tool_call


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}", "X-FITT-Client": "cli"}


def _content(body: dict[str, Any]) -> str:
    msg = body.get("choices", [{}])[0].get("message", {})
    return str(msg.get("content") or "")


@pytest.fixture
def app(tmp_path: Path) -> Any:
    return create_app(build_test_config(tmp_path))


def _register_noting_tool(app: Any) -> None:
    async def _impl(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult.ok(
            "created cron 'x'",
            user_note="This fires Fri 21 Aug at 11:58 (EDT).",
            user_note_probe="11:58",
        )

    app.state.tool_registry.register(
        Tool(
            name="pretend_cron_add",
            description="stands in for cron_add",
            schema={"type": "object", "properties": {}},
            callable=_impl,
            default_bucket=ApprovalBucket.AUTO,
        )
    )


# --------------------------------------------------------------- tool-loop path


def test_a_user_note_reaches_the_non_streaming_body(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported failure. Note the request must send tool_choice: the
    tool loop is opt-in, and a request without it takes the plain-chat path
    — which is why an earlier check of this appeared to pass."""
    from gateway.e2e_driver import auto_approve_for_eval

    _register_noting_tool(app)
    auto_approve_for_eval(app)

    calls = iter(
        [
            make_response(tool_calls=[make_tool_call("c1", "pretend_cron_add", {})]),
            make_response(content="OK. I've set that reminder in 10 minutes."),
        ]
    )

    async def fake(**kwargs: Any) -> Any:
        return next(calls)

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = TestClient(app).post(
        "/v1/chat/completions",
        json={
            "model": "fitt-default",
            "messages": [{"role": "user", "content": "remind me in 10 minutes"}],
            "tool_choice": "auto",
        },
        headers=_auth(),
    )

    assert r.status_code == 200, r.text
    assert "11:58" in _content(r.json()), (
        f"the appended user_note never reached the delivered body — got {_content(r.json())!r}"
    )


def test_the_template_token_strip_reaches_the_non_streaming_body(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The oldest open item in observed-issues, and it was only half-fixed:
    stripped in assistant_text, raw in the body a client reads."""
    from gateway.e2e_driver import auto_approve_for_eval

    _register_noting_tool(app)
    auto_approve_for_eval(app)

    calls = iter(
        [
            make_response(tool_calls=[make_tool_call("c1", "pretend_cron_add", {})]),
            make_response(content="Done.<|tool_response>"),
        ]
    )

    async def fake(**kwargs: Any) -> Any:
        return next(calls)

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = TestClient(app).post(
        "/v1/chat/completions",
        json={
            "model": "fitt-default",
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": "auto",
        },
        headers=_auth(),
    )

    assert "<|tool_response>" not in _content(r.json()), _content(r.json())


def test_a_turn_with_no_corrections_is_left_alone(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The join must be a no-op when there's nothing to change, or every
    reply becomes a place a bug can hide."""

    async def fake(**kwargs: Any) -> Any:
        return make_response(content="Just a plain answer.")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = TestClient(app).post(
        "/v1/chat/completions",
        json={
            "model": "fitt-default",
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": "auto",
        },
        headers=_auth(),
    )

    assert _content(r.json()) == "Just a plain answer."


# --------------------------------------------------------------- plain-chat path
#
# No tools / tool_choice in the request: Telegram and Open WebUI's ordinary
# conversation. This path has its own extractor, which never stripped at
# all — so "the one funnel every non-streaming reply passes through" was
# two funnels, one of them uncorrected.


def test_plain_chat_also_strips_template_tokens(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(**kwargs: Any) -> Any:
        assert not kwargs.get("tools"), "this test must exercise the plain-chat path"
        return make_response(content="Hello there.<|tool_response>")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = TestClient(app).post(
        "/v1/chat/completions",
        json={"model": "fitt-default", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(),
    )

    assert r.status_code == 200, r.text
    assert "<|tool_response>" not in _content(r.json()), _content(r.json())
    assert "Hello there." in _content(r.json())


def test_a_tool_calls_only_response_is_not_erased(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard on the join itself: writing an empty assistant_text into the
    body would destroy a tool_calls envelope a router-mode client needs."""
    from gateway.chat import _apply_assistant_text

    body = {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "tool_calls": [{"id": "c1", "type": "function"}],
                },
            }
        ]
    }

    _apply_assistant_text(body, "")

    assert "content" not in body["choices"][0]["message"]
    assert body["choices"][0]["message"]["tool_calls"]
