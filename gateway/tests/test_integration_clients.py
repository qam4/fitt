"""Phase 4 Task 17 — end-to-end integration tests.

These exercise the gateway's tool-forwarding path with realistic
client shapes. Two scenarios:

* **17a — Telegram-style.** A bot-shape request (``tool_choice:
  "auto"``, ``stream: True``, ``X-FITT-Client: telegram``) hits
  the gateway. The model replies with a ``read_file`` tool call
  against an *SSH-routed* project. We stub the execution backend
  so ``read_file`` resolves without touching a real satellite.
  The gateway re-dispatches with the tool result, the model
  finishes, and the bot sees a streaming chunk carrying the
  final text. Audit log gains one entry.

* **17b — Continue-style IDE.** An IDE-shape request (``X-FITT-
  Client: ide``) arrives with its *own* ``tools`` array (e.g.
  Continue's ``builtin_readFile``). FITT preserves the client's
  tools, appends its own, and when the model picks a
  client-owned name, the gateway returns a structured
  tool_result error naming the missing tool rather than
  executing something. (Full forward-back-to-client behaviour
  is future work tracked as the open half of task 16c; this
  test pins the current contract so the regression is visible
  the day we change it.)

Both tests stub ``litellm.acompletion`` so no real LLM traffic
happens, and both use the existing test-config fixture with an
in-process ExecutionBackend whose ``run_shell`` is replaced with
an in-memory function.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.projects import Project
from gateway.tools.backend import ShellResult

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
    in_tok: int = 5,
    out_tok: int = 3,
) -> Any:
    """Compat shim → shared ``make_response``."""
    return make_response(
        content=content,
        tool_calls=tool_calls,
        in_tok=in_tok,
        out_tok=out_tok,
    )


@dataclass
class _CapturedCall:
    """One call the stub intercepted, for assertion."""

    kwargs: dict[str, Any]


def _install_stub_backend(app: Any, payload: str) -> list[tuple[list[str], Project]]:
    """Replace the execution backend's ``run_shell`` with an
    in-memory stub that records invocations and returns ``payload``
    as stdout. Returns the list invocations mutate in-place."""
    recorded: list[tuple[list[str], Project]] = []

    real_backend = app.state.execution_backend

    async def _fake_run_shell(project: Project, cmd: list[str], **_: Any) -> ShellResult:
        recorded.append((cmd, project))
        return ShellResult(exit=0, stdout=payload, stderr="", timed_out=False)

    real_backend.run_shell = _fake_run_shell  # type: ignore[method-assign]
    return recorded


@pytest.fixture
def telegram_app(tmp_path: Path) -> tuple[Any, TestClient, list[tuple[list[str], Project]]]:
    """Build a gateway app with an SSH-routed project and a stubbed
    execution backend. Returns (app, client, recorded_shell_calls)."""
    cfg = build_test_config(tmp_path)
    app = create_app(cfg)

    # An SSH-routed project forces the ExecutionBackend down the
    # remote path, which is the interesting-for-Task-17a shape.
    # Path is cosmetic here because the backend is stubbed, but we
    # still record it so the stub can assert on the project object.
    app.state.project_registry.add(
        Project(
            name="satellite-repo",
            ssh_host="satellite.tailnet",
            path="/home/fred/satellite-repo",
            test_command="pytest -q",
        )
    )
    # Mirror the real Telegram setup: the operator typically calls
    # `fitt session new probe` before asking the bot anything, so
    # the gateway's session-validation middleware recognises the
    # X-FITT-Session header the bot sends.
    app.state.session_registry.create("probe")

    recorded = _install_stub_backend(app, payload="# Satellite\nHello from the satellite.\n")
    return app, TestClient(app), recorded


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


# --------------------------------------------------------------- 17a


def test_telegram_end_to_end_streams_final_reply(
    telegram_app: tuple[Any, TestClient, list[tuple[list[str], Project]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 17a. Full Telegram-shape request: streaming ON,
    ``tool_choice: auto``, ``X-FITT-Client: telegram``. The model
    picks ``read_file`` on an SSH project, the gateway resolves
    via the (stubbed) SSH backend, then the model finishes with
    the file content. The bot sees the final reply as a single
    streaming chunk + stop."""
    app, client, recorded = telegram_app

    calls: list[_CapturedCall] = []

    async def fake_llm(**kwargs: Any) -> Any:
        calls.append(_CapturedCall(kwargs=kwargs))
        if len(calls) == 1:
            return _fake_response(
                tool_calls=[
                    _tool_call(
                        "call-1",
                        "read_file",
                        {"project": "satellite-repo", "path": "README.md"},
                    )
                ]
            )
        return _fake_response(content="Summary: hello from the satellite.")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_llm)

    # This is the exact shape the bot's GatewayClient sends.
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [
                {"role": "user", "content": "read the README and summarize"},
            ],
            "stream": True,
            "tool_choice": "auto",
        },
        headers={**_auth(), "X-FITT-Client": "telegram", "X-FITT-Session": "probe"},
    )

    assert r.status_code == 200, r.text
    assert r.headers.get("content-type", "").startswith("text/event-stream")

    # Parse the SSE frames. Two chunks: content delta, then stop.
    frames = []
    for raw in r.text.splitlines():
        if raw.startswith("data: "):
            payload = raw[len("data: ") :]
            if payload == "[DONE]":
                continue
            frames.append(json.loads(payload))
    assert len(frames) == 2, frames
    first = frames[0]
    assert first["choices"][0]["delta"]["content"] == "Summary: hello from the satellite."
    second = frames[1]
    assert second["choices"][0]["finish_reason"] == "stop"

    # The tool loop ran exactly two LLM passes.
    assert len(calls) == 2

    # The first pass carried FITT's injected tools AND a system
    # prompt with the capability block.
    first_kwargs = calls[0].kwargs
    tool_names = [
        t.get("function", {}).get("name")
        for t in first_kwargs.get("tools") or []
        if isinstance(t, dict)
    ]
    assert "read_file" in tool_names
    assert first_kwargs.get("stream") is False  # forced off in tool loop
    system = next(
        (m for m in first_kwargs["messages"] if m.get("role") == "system"),
        None,
    )
    assert system is not None and "[Capabilities]" in system["content"]

    # The stubbed SSH backend was hit, routed to the right project.
    assert len(recorded) == 1
    _, project = recorded[0]
    assert project.name == "satellite-repo"
    assert project.ssh_host == "satellite.tailnet"

    # The tool-result message fed back into the second LLM pass
    # carries the file content from the (stubbed) satellite.
    second_msgs = calls[1].kwargs["messages"]
    tool_msg = next(m for m in second_msgs if m.get("role") == "tool")
    assert tool_msg["tool_call_id"] == "call-1"
    assert "Hello from the satellite" in tool_msg["content"]

    # One audit entry, tagged with the telegram client and session.
    entries = app.state.audit.iter_entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["tool"] == "read_file"
    assert entry["client"] == "telegram"
    assert entry["session_key"] == "probe"
    assert entry["ok"] is True


# --------------------------------------------------------------- 17b


def test_ide_continue_shape_preserves_client_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 17b. A Continue-style IDE request ships with its own
    ``tools`` array (mock Continue naming: ``builtin_readFile``).
    FITT must:

    * Preserve the client's tools, with them coming *first* in the
      list so client-owned names take lookup precedence.
    * Append FITT's tools behind them.
    * When the model invokes a client-owned tool name, *not*
      execute anything (we don't own that name) and return a
      structured tool_result error that names the missing tool —
      the model can then either retry with a FITT tool or give up.

    When (future) full forward-to-client support lands, this test
    will need to be updated to assert the forwarded shape. For
    now, it pins the current contract so any drift is visible.
    """
    cfg = build_test_config(tmp_path)
    app = create_app(cfg)
    # Hub-local project so the file tools have somewhere to resolve.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Hub\nHello from the hub.\n", encoding="utf-8")
    app.state.project_registry.add(
        Project(name="hub", ssh_host="", path=str(repo), test_command="pytest -q"),
    )
    client = TestClient(app)

    calls: list[_CapturedCall] = []

    async def fake_llm(**kwargs: Any) -> Any:
        calls.append(_CapturedCall(kwargs=kwargs))
        if len(calls) == 1:
            # Model tries the IDE's tool first (client-owned).
            return _fake_response(
                tool_calls=[
                    _tool_call(
                        "call-ide-1",
                        "builtin_readFile",
                        {"filepath": "README.md"},
                    )
                ]
            )
        if len(calls) == 2:
            # After getting back "not registered", the model falls
            # back to FITT's read_file against the hub project.
            return _fake_response(
                tool_calls=[
                    _tool_call(
                        "call-fitt-1",
                        "read_file",
                        {"project": "hub", "path": "README.md"},
                    )
                ]
            )
        return _fake_response(content="Done — hub README read.")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_llm)

    # Continue-style: client supplies its own tool in addition to
    # asking FITT for tool-enabled mode via tool_choice.
    continue_tool = {
        "type": "function",
        "function": {
            "name": "builtin_readFile",
            "description": "Continue's built-in file reader.",
            "parameters": {
                "type": "object",
                "properties": {"filepath": {"type": "string"}},
                "required": ["filepath"],
            },
        },
    }

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "read the hub README"}],
            "tools": [continue_tool],
            "tool_choice": "auto",
        },
        headers={**_auth(), "X-FITT-Client": "ide", "X-FITT-Session": "main"},
    )
    assert r.status_code == 200, r.text

    body = r.json()
    assert body["choices"][0]["message"]["content"] == "Done — hub README read."

    # Three dispatches: (1) with client + FITT tools exposed, (2)
    # after the "unknown tool" error, (3) after the successful
    # FITT read_file call.
    assert len(calls) == 3

    # Assert the shape of what the upstream model saw on dispatch 1.
    first = calls[0].kwargs
    tools = first.get("tools") or []
    names = [t.get("function", {}).get("name") for t in tools if isinstance(t, dict)]
    assert names[0] == "builtin_readFile", "client-supplied tools must come first"
    assert "read_file" in names, "FITT's own tools must be appended"
    assert "git_status" in names, "FITT's full registered set is appended"

    # Dispatch 2 got a tool-result message explaining the client
    # tool wasn't registered with FITT — that's the current
    # contract (pin it now so full-forward support lands as an
    # intentional change).
    second_msgs = calls[1].kwargs["messages"]
    unknown_tool_msg = next(m for m in second_msgs if m.get("role") == "tool")
    assert unknown_tool_msg["tool_call_id"] == "call-ide-1"
    assert "not registered" in unknown_tool_msg["content"]
    assert "builtin_readFile" in unknown_tool_msg["content"]

    # Dispatch 3 got the actual file content from FITT's read_file.
    third_msgs = calls[2].kwargs["messages"]
    hub_tool_msg = next(
        m for m in third_msgs if m.get("role") == "tool" and m.get("tool_call_id") == "call-fitt-1"
    )
    assert "Hello from the hub" in hub_tool_msg["content"]

    # Audit captured both calls — the attempted unknown one and
    # the successful FITT one.
    entries = app.state.audit.iter_entries()
    tools_seen = {e["tool"] for e in entries}
    assert "read_file" in tools_seen
    # Unknown tools are audited too (visibility into what the
    # model tried), even though they didn't execute.
    assert "(unknown)" in tools_seen or "builtin_readFile" in tools_seen
    # And the `ide` client tag survived from the header.
    for e in entries:
        assert e["client"] == "ide"
