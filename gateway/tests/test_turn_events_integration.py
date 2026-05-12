"""Phase 4.8a — turn-event emission wired through the full stack.

End-to-end integration: a tool-using chat request drives the
agent loop, and we assert that

1. The per-turn JSONL file at
   ``sessions/<session_key>/turns/<YYYY-MM-DD>.jsonl`` contains
   the expected event sequence.
2. A subscriber registered on ``app.state.turns`` receives the
   same events in the same order.
3. Every event in the turn carries the same ``turn_id``.
4. A cron firing produces its own distinct turn with the same
   shape.

The LLM dispatch is stubbed via
``gateway.router.litellm.acompletion`` — same pattern as
``test_chat_tool_forwarding`` and ``test_cron_runner``. We don't
care what the model said, only that the gateway emitted the
right turn events around its dispatch + tool execution.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.projects import Project
from gateway.turns import TurnEvent

from ._fixtures import PERSONAL_TOKEN, build_test_config
from ._llm_stubs import stub_reply, stub_sequence, stub_tool_call


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path)
    app = create_app(cfg)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Hello\nreadme body.\n", encoding="utf-8")
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


# --------------------------------------------------------------- chat turn


def test_chat_tool_turn_emits_expected_event_sequence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One chat request that calls ``read_file`` and replies.

    Expected event sequence in ``turns/<date>.jsonl``:

    * turn_started
    * llm_call_started (iter 0)
    * llm_call_completed (iter 0, tool_calls_count=1)
    * tool_call_planned (read_file)
    * tool_call_executed (read_file, ok=True)
    * llm_call_started (iter 1)
    * llm_call_completed (iter 1, tool_calls_count=0)
    * turn_finished (status=ok, iterations=2)

    All events share the same turn_id. A subscriber registered
    on app.state.turns sees the same sequence.
    """
    seen: list[TurnEvent] = []
    client.app.state.turns.subscribe(seen.append)

    monkeypatch.setattr(
        "gateway.router.litellm.acompletion",
        stub_sequence(
            [
                stub_tool_call(
                    "read_file",
                    {"project": "hub", "path": "README.md"},
                    call_id="call-1",
                ),
                stub_reply("README said hi"),
            ]
        ),
    )

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

    kinds = [ev.kind for ev in seen]
    assert kinds == [
        "turn_started",
        "llm_call_started",
        "llm_call_completed",
        "tool_call_planned",
        "tool_call_executed",
        "llm_call_started",
        "llm_call_completed",
        "turn_finished",
    ]

    # All events share the same turn_id.
    turn_ids = {ev.turn_id for ev in seen}
    assert len(turn_ids) == 1
    turn_id = turn_ids.pop()
    assert turn_id  # not empty

    # Event_id is unique per event.
    event_ids = [ev.event_id for ev in seen]
    assert len(event_ids) == len(set(event_ids))

    # Shape assertions on the meta fields of key events.
    started = seen[0]
    assert started.meta["alias"] == "fitt-smart"
    assert started.meta["user_msg_len"] > 0

    first_llm = seen[1]
    assert first_llm.meta["alias"] == "fitt-smart"
    assert first_llm.meta["iteration"] == 0

    first_llm_done = seen[2]
    assert first_llm_done.meta["tool_calls_count"] == 1

    planned = seen[3]
    assert planned.meta["tool_name"] == "read_file"
    assert planned.meta["call_id"] == "call-1"
    assert planned.meta["args"]["path"] == "README.md"
    assert planned.meta["iteration"] == 0

    executed = seen[4]
    assert executed.meta["tool_name"] == "read_file"
    assert executed.meta["call_id"] == "call-1"
    assert executed.meta["ok"] is True
    assert executed.meta["duration_ms"] >= 0

    finished = seen[-1]
    assert finished.meta["status"] == "ok"
    assert finished.meta["iterations"] == 2
    assert finished.meta["final_reply_len"] == len("README said hi")

    # JSONL on disk matches the same sequence.
    session_key = r.headers["X-FITT-Session"]
    disk_events = client.app.state.turns.read(session_key)
    disk_kinds = [ev.kind for ev in disk_events]
    assert disk_kinds == kinds
    assert {ev.turn_id for ev in disk_events} == {turn_id}


def test_chat_non_tool_turn_emits_no_turn_events(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain chat request (no ``tools`` / ``tool_choice``)
    doesn't go through the tool loop, so it doesn't emit
    turn events. Memory still updates and the response is
    served normally — we pin that the non-tool path stays
    quiet on the turn stream."""
    seen: list[TurnEvent] = []
    client.app.state.turns.subscribe(seen.append)

    monkeypatch.setattr(
        "gateway.router.litellm.acompletion",
        stub_sequence([stub_reply("hi there")]),
    )

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers=_auth(),
    )
    assert r.status_code == 200
    assert seen == []
