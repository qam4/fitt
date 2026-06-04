"""Tests for Phase 4 Task 16 tool forwarding in the chat handler.

Monkeypatches ``litellm.acompletion`` to return canned responses
with tool-call shapes, so we can exercise the tool-execution loop
end-to-end without any real LLM traffic.

What we exercise:

* Requests without a ``tools`` key (plain chat) go down the old
  streaming-or-not path, unchanged.
* Requests with a ``tools`` or ``tool_choice`` key trigger the
  tool-loop dispatch, which forces non-streaming.
* FITT-registered tools are appended to the ``tools`` array the
  upstream model sees; client-supplied tools are preserved.
* When the model returns tool_calls, the loop executes them,
  inserts tool-result messages, and re-dispatches.
* When the model returns a natural ``stop``, the loop terminates
  and the last response is returned.
* Exceeding the iteration cap returns 504.
* Hallucinated tool names come back as tool_result errors rather
  than 500s.
* Approval-rejected calls come back as tool_result errors.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.projects import Project

from ._fixtures import PERSONAL_TOKEN, build_test_config
from ._llm_stubs import make_response, make_tool_call

# --------------------------------------------------------------- helpers


def _tool_call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Compat shim → shared ``make_tool_call``."""
    return make_tool_call(call_id, name, args)


def _fake_response(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str | None = None,
    in_tok: int = 5,
    out_tok: int = 3,
) -> Any:
    """Compat shim → shared ``make_response``."""
    return make_response(
        content=content,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        in_tok=in_tok,
        out_tok=out_tok,
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path)
    app = create_app(cfg)

    # Register a hub-local project so the file/git tools have somewhere
    # to resolve to. Putting it under tmp_path keeps the test
    # hermetic.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Hello\nthe readme.\n", encoding="utf-8")
    app.state.project_registry.add(
        Project(
            name="hub",
            ssh_host="",
            path=str(repo),
            test_command="pytest -q",
        )
    )
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


# --------------------------------------------------------------- opt-in gating


def test_plain_chat_does_not_trigger_tool_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a `tools`/`tool_choice` key, the request flows through
    the original path - forced non-streaming would appear as a
    missing X-FITT-Backend header or a structural diff; we assert
    the simplest proxy: `tools` was not present in the upstream
    kwargs."""
    captured: dict[str, Any] = {}

    async def fake(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _fake_response(content="hi")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={"model": "fitt-smart", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert "tools" not in captured


def test_tools_present_triggers_tool_injection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`tools: []` from the client opts into the tool loop; FITT's
    registered tools get appended to what the model sees."""
    captured: dict[str, Any] = {}

    async def fake(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _fake_response(content="done")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [],
            "tool_choice": "auto",
        },
        headers=_auth(),
    )
    assert r.status_code == 200
    assert "tools" in captured
    names = [t.get("function", {}).get("name") for t in captured["tools"] if isinstance(t, dict)]
    assert "read_file" in names
    assert "git_status" in names
    # stream was forced off for the tool-loop request.
    assert captured.get("stream") is False


def test_client_supplied_tools_preserved(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client's own tools go first so their names take precedence."""
    captured: dict[str, Any] = {}

    async def fake(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _fake_response(content="done")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    client_tool = {
        "type": "function",
        "function": {"name": "client_special", "description": "x", "parameters": {}},
    }
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [client_tool],
        },
        headers=_auth(),
    )
    assert r.status_code == 200
    names = [t.get("function", {}).get("name") for t in captured["tools"] if isinstance(t, dict)]
    assert names[0] == "client_special"
    assert "read_file" in names


# --------------------------------------------------------------- execution loop


def test_tool_call_is_executed_and_result_returned(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 1 returns tool_calls; round 2 returns a final message.
    We assert on the loop behaviour by collecting upstream calls.
    """
    calls: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if len(calls) == 1:
            # First call: ask for read_file on README.md
            return _fake_response(
                tool_calls=[
                    _tool_call(
                        "call-1",
                        "read_file",
                        {"project": "hub", "path": "README.md"},
                    )
                ]
            )
        # Second call: model wraps up
        return _fake_response(content="README says: # Hello")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "read the readme"}],
            "tool_choice": "auto",
        },
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "README says: # Hello"
    # Two dispatches: one to produce the tool call, one with the
    # tool result injected.
    assert len(calls) == 2
    # The second dispatch's messages include the tool result.
    msgs = calls[1]["messages"]
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "call-1" for m in msgs)
    tool_msg = next(m for m in msgs if m.get("role") == "tool")
    assert "Hello" in tool_msg["content"]


def test_unknown_tool_produces_structured_error_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Model hallucinates a tool that isn't in the registry. The
    error comes back in-band as a tool_result, the model finishes
    the turn, and the HTTP status is still 200."""
    calls: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if len(calls) == 1:
            return _fake_response(tool_calls=[_tool_call("c1", "launch_missile", {})])
        return _fake_response(content="ok I gave up")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": "auto",
        },
        headers=_auth(),
    )
    assert r.status_code == 200
    # The tool-result message the second call receives carries
    # the "not registered" error message.
    msgs = calls[1]["messages"]
    tool_msg = next(m for m in msgs if m.get("role") == "tool")
    assert "not registered" in tool_msg["content"]


def test_iteration_cap_returns_504(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Model never stops calling tools - we bail with 504 tool_loop_exhausted."""
    calls: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> Any:
        calls.append(kwargs)
        # Always ask for another tool call.
        return _fake_response(
            tool_calls=[
                _tool_call(
                    f"c{len(calls)}",
                    "read_file",
                    {"project": "hub", "path": "README.md"},
                )
            ]
        )

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "loop"}],
            "tool_choice": "auto",
        },
        headers=_auth(),
    )
    assert r.status_code == 504
    assert r.json()["error"]["type"] == "tool_loop_exhausted"
    # Iteration cap is 10.
    assert len(calls) == 10


def test_malformed_tool_call_arguments_surface_as_tool_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Model emits a tool_call with invalid JSON arguments. The
    tool-result message carries a 'not valid JSON' error; no 500."""
    calls: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if len(calls) == 1:
            return _fake_response(
                tool_calls=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "not json"},
                    }
                ]
            )
        return _fake_response(content="giving up")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": "auto",
        },
        headers=_auth(),
    )
    assert r.status_code == 200
    msgs = calls[1]["messages"]
    tool_msg = next(m for m in msgs if m.get("role") == "tool")
    assert "not valid JSON" in tool_msg["content"]


def test_stream_wanted_returns_streaming_chunk_shape(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Client set stream=True alongside tool_choice. Tool loop runs
    non-streaming internally, then wraps the final response as a
    streaming chunk (`choices[0].delta.content`, NOT
    `choices[0].message.content`) so SSE-consuming clients can
    parse it with their normal delta-accumulating loop."""

    async def fake(**_: Any) -> Any:
        return _fake_response(content="done")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": "auto",
            "stream": True,
        },
        headers=_auth(),
    )
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("text/event-stream")

    # Parse the SSE frames.
    frames = []
    for raw in r.text.splitlines():
        if raw.startswith("data: "):
            payload = raw[len("data: ") :]
            if payload == "[DONE]":
                continue
            frames.append(json.loads(payload))

    # Expect two chunks: content delta, then final stop.
    assert len(frames) == 2, frames
    # First chunk carries the assistant content as a delta.
    first = frames[0]
    assert first["object"] == "chat.completion.chunk"
    delta = first["choices"][0]["delta"]
    assert delta.get("content") == "done"
    assert delta.get("role") == "assistant"
    # Second chunk is the terminator.
    second = frames[1]
    assert second["choices"][0]["finish_reason"] == "stop"


# --------------------------------------------------------------- capture timestamps


def test_tool_loop_capture_records_walltime_started_at(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the turn capture's ``started_at`` must be a
    wall-clock Unix timestamp, not a ``perf_counter`` monotonic
    value.

    The bug (2026-06-03): the chat handler passed its
    ``perf_counter`` start straight into the capture builder,
    while the builder set ``finished_at`` from ``time.time()``.
    Mixing the two clocks made the dashboard show ~1.78e9 s
    latency and "20597d ago" ages. We assert started_at is a
    recent epoch and the derived latency is sane (sub-minute for
    this instant turn)."""
    import time as _time

    calls: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if len(calls) == 1:
            return _fake_response(
                tool_calls=[
                    _tool_call("call-1", "read_file", {"project": "hub", "path": "README.md"})
                ]
            )
        return _fake_response(content="README says: # Hello")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    before = _time.time()
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "read the readme"}],
            "tool_choice": "auto",
        },
        headers=_auth(),
    )
    after = _time.time()
    assert r.status_code == 200, r.text

    turn_id = r.headers.get("X-FITT-Turn-Id")
    assert turn_id, "tool-loop turn should carry X-FITT-Turn-Id"

    store = client.app.state.turn_capture
    cap = store.read("main", turn_id)
    assert cap is not None, "tool-loop turn should have been captured"

    # started_at is a wall-clock epoch within the request window
    # (allow a small slack for the monotonic-to-wall conversion).
    assert before - 1.0 <= cap.started_at <= after + 1.0
    # finished_at is at/after start, and the turn was instant, so
    # the derived latency is well under a minute — never ~1.78e9 s.
    latency_s = cap.finished_at - cap.started_at
    assert 0.0 <= latency_s < 60.0
