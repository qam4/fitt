"""Phase 5 — tool-turn memory persistence.

Covers the invariants from design doc section "Tool-turn
persistence":

* A tool-using turn written via ``append_turn(tool_calls=[...])``
  produces the 4-block on-disk shape: user, assistant
  tool_calls, tool <name>+, final assistant.
* Loading that turn back produces an OpenAI-shape message
  sequence with ``role: assistant + tool_calls``, ``role: tool``
  per call, and a final ``role: assistant``.
* A Phase-2-shape file (only user/assistant headers) loads
  identically — the chat-only path stays back-compat forever.
* Unknown-role headers degrade gracefully.
* The tool-call args round-trip byte-accurate through write +
  read (2026-05-11 correction to the original lossy-string
  design; see docs/observed-issues.md).

The ``test_session_poisoning_lifecycle`` e2e test is what pins
the user-facing outcome ("stale refusal doesn't reach the
model"); these unit tests pin the machinery behind it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.memory import MemoryStore, PersistedToolCall


def _make_store(tmp_path: Path) -> MemoryStore:
    identity_dir = tmp_path / "identity"
    sessions_dir = tmp_path / "sessions"
    identity_dir.mkdir()
    return MemoryStore(
        identity_dir=identity_dir,
        sessions_dir=sessions_dir,
        max_history_chars=24_000,
        enabled=True,
    )


# --------------------------------------------------------------- PersistedToolCall


def test_persisted_tool_call_render_call_bullet() -> None:
    call = PersistedToolCall(
        tool_name="project_shell",
        args={"project": "hub", "command": "ls"},
        result_status="ok",
        result_summary="",
    )
    expected = '- project_shell\n```json\n{"project": "hub", "command": "ls"}\n```'
    assert call.render_call_bullet() == expected


def test_persisted_tool_call_render_call_bullet_empty_args() -> None:
    """A tool with no arguments still renders a complete
    fenced-JSON block (empty object). Reader expects the
    fence to always be present."""
    call = PersistedToolCall(
        tool_name="list_capabilities",
        args={},
        result_status="ok",
        result_summary="",
    )
    bullet = call.render_call_bullet()
    assert bullet.startswith("- list_capabilities\n")
    assert "```json" in bullet
    assert "{}" in bullet
    assert bullet.endswith("\n```")


def test_persisted_tool_call_preserves_long_args_verbatim() -> None:
    """Args are no longer truncated at persist time. A long
    command string survives the round-trip exactly. This is
    the property the 2026-05-11 format change added — the
    old format truncated at 80 chars and lost information,
    which broke reconstruction on reload."""
    long_command = "x" * 500
    call = PersistedToolCall(
        tool_name="project_shell",
        args={"command": long_command},
        result_status="ok",
        result_summary="",
    )
    bullet = call.render_call_bullet()
    assert long_command in bullet


def test_persisted_tool_call_render_result_body_ok_only() -> None:
    call = PersistedToolCall(
        tool_name="read_file",
        args={"path": "x"},
        result_status="ok",
        result_summary="",
    )
    assert call.render_result_body() == "ok"


def test_persisted_tool_call_render_result_body_with_summary() -> None:
    call = PersistedToolCall(
        tool_name="project_shell",
        args={"command": "false"},
        result_status="exit=1",
        result_summary="command failed",
    )
    assert call.render_result_body() == "exit=1: command failed"


# --------------------------------------------------------------- append / load


def test_chat_only_turn_writes_phase2_shape(tmp_path: Path) -> None:
    """Without ``tool_calls``, the format stays the Phase 2
    two-block shape. Nothing new on disk, nothing new on reload."""
    store = _make_store(tmp_path)
    store.append_turn("main", "hello", "hi there")
    ctx = store.load_context("main")
    assert ctx.history_messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]


def test_tool_turn_writes_four_blocks(tmp_path: Path) -> None:
    """Sanity: writing a tool-using turn produces the expected
    headers on disk. Easy to grep; easy to human-inspect."""
    store = _make_store(tmp_path)
    calls = [
        PersistedToolCall(
            tool_name="project_shell",
            args={"project": "hub", "command": "ls"},
            result_status="ok",
            result_summary="",
        ),
    ]
    store.append_turn("main", "list", "there's foo.txt", tool_calls=calls)

    raw = store.history_path("main").read_text(encoding="utf-8")
    # Four blocks in the right order.
    user_idx = raw.find("user")
    calls_idx = raw.find("assistant tool_calls")
    tool_idx = raw.find("tool project_shell")
    final_idx = raw.rfind("assistant")
    assert 0 <= user_idx < calls_idx < tool_idx < final_idx, (
        "headers should appear in the order: user → assistant "
        f"tool_calls → tool <name> → assistant; got positions "
        f"{user_idx} / {calls_idx} / {tool_idx} / {final_idx}"
    )
    # Tool-call bullet uses the name + JSON fence shape.
    assert "- project_shell" in raw
    assert "```json" in raw
    assert '"project": "hub"' in raw
    assert '"command": "ls"' in raw
    # Tool block body carries the status.
    assert "ok" in raw


def test_tool_turn_loads_as_openai_shape(tmp_path: Path) -> None:
    """Round-trip: the messages we get back carry the
    OpenAI-shape ``tool_calls`` on assistant + ``tool_call_id``
    on tool. The ids pair correctly (same string on both
    halves) so the LLM receives a valid conversation."""
    store = _make_store(tmp_path)
    calls = [
        PersistedToolCall(
            tool_name="project_shell",
            args={"project": "hub", "command": "ls"},
            result_status="ok",
            result_summary="",
        ),
    ]
    store.append_turn("main", "list", "there's foo.txt", tool_calls=calls)
    ctx = store.load_context("main")
    msgs = ctx.history_messages

    assert len(msgs) == 4
    assert msgs[0] == {"role": "user", "content": "list"}

    # Assistant+tool_calls.
    assistant_call = msgs[1]
    assert assistant_call["role"] == "assistant"
    assert assistant_call["content"] == ""
    assert isinstance(assistant_call["tool_calls"], list)
    assert len(assistant_call["tool_calls"]) == 1
    call = assistant_call["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"]["name"] == "project_shell"
    assert "id" in call
    assert call["id"].startswith("persisted-")

    # Tool role entry — id matches.
    tool_msg = msgs[2]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == call["id"]
    assert tool_msg["content"] == "ok"

    # Final assistant reply.
    assert msgs[3] == {"role": "assistant", "content": "there's foo.txt"}


def test_tool_call_arguments_round_trip_byte_accurate(
    tmp_path: Path,
) -> None:
    """The 2026-05-11 fix: args must round-trip exactly
    through write → load. The old lossy-string format
    truncated at 80 chars and stuffed the un-parseable
    leftover into a ``_persisted_args`` placeholder that
    then leaked into the model's context, creating a self-
    reinforcing poisoning loop (see
    docs/observed-issues.md). This test pins the fix: the
    reconstructed ``arguments`` JSON parses back to the
    exact dict that was persisted, and there is NO
    ``_persisted_args`` key anywhere in the output."""
    store = _make_store(tmp_path)
    original_args = {
        "project": "home-ai-cluster",
        "path": "README.md",
        "line_range": [1, 200],
        "nested": {"encoding": "utf-8", "follow_symlinks": False},
        "long_value": "a" * 300,
    }
    calls = [
        PersistedToolCall(
            tool_name="read_file",
            args=original_args,
            result_status="ok",
            result_summary="",
        ),
    ]
    store.append_turn("main", "read it", "done", tool_calls=calls)

    ctx = store.load_context("main")
    assistant_call = ctx.history_messages[1]
    reconstructed_args_json = assistant_call["tool_calls"][0]["function"]["arguments"]
    reconstructed_args = json.loads(reconstructed_args_json)
    assert reconstructed_args == original_args

    # Explicit guard: _persisted_args placeholder must not
    # appear anywhere in the reconstructed messages. The
    # poisoning loop begins the moment this key lands in
    # history the model can read.
    serialized_all = json.dumps(ctx.history_messages)
    assert "_persisted_args" not in serialized_all


def test_tool_call_arguments_round_trip_with_special_chars(
    tmp_path: Path,
) -> None:
    """Shell commands carry quotes, newlines, and backslashes.
    The JSON-on-disk format must handle these without
    ambiguity. The old repr-based summary could lose track
    of quote types mid-string."""
    store = _make_store(tmp_path)
    original_args = {
        "command": 'grep -rn "TODO\\|FIXME" src/ | head -5',
        "env": {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"},
    }
    calls = [
        PersistedToolCall(
            tool_name="project_shell",
            args=original_args,
            result_status="ok",
            result_summary="",
        ),
    ]
    store.append_turn("main", "grep", "done", tool_calls=calls)

    ctx = store.load_context("main")
    assistant_call = ctx.history_messages[1]
    reconstructed = json.loads(assistant_call["tool_calls"][0]["function"]["arguments"])
    assert reconstructed == original_args


def test_multiple_tool_calls_pair_correctly(tmp_path: Path) -> None:
    """Two tool calls → two tool blocks → two ``role: tool``
    entries, each with its own id matching the corresponding
    call's id. If the pairing drifts the LLM sees
    "tool_call_id references unknown id" and breaks."""
    store = _make_store(tmp_path)
    calls = [
        PersistedToolCall(
            tool_name="read_file",
            args={"path": "a.txt"},
            result_status="ok",
            result_summary="",
        ),
        PersistedToolCall(
            tool_name="read_file",
            args={"path": "b.txt"},
            result_status="ok",
            result_summary="",
        ),
    ]
    store.append_turn("main", "read both", "done", tool_calls=calls)

    ctx = store.load_context("main")
    msgs = ctx.history_messages
    # user, assistant+tool_calls, tool, tool, assistant.
    assert len(msgs) == 5
    assistant_call = msgs[1]
    id_a = assistant_call["tool_calls"][0]["id"]
    id_b = assistant_call["tool_calls"][1]["id"]
    assert id_a != id_b
    assert msgs[2]["tool_call_id"] == id_a
    assert msgs[3]["tool_call_id"] == id_b


def test_tool_result_with_error_preserves_status_and_summary(
    tmp_path: Path,
) -> None:
    """An exit=N + summary from a failing tool call persists
    as ``exit=N: summary`` in the tool block body, preserving
    both pieces so a model reloading can reason about the
    specific failure — not just "something went wrong."""
    store = _make_store(tmp_path)
    calls = [
        PersistedToolCall(
            tool_name="project_shell",
            args={"command": "false"},
            result_status="exit=1",
            result_summary="command failed: exit 1",
        ),
    ]
    store.append_turn("main", "try false", "command failed", tool_calls=calls)

    ctx = store.load_context("main")
    tool_msg = next(m for m in ctx.history_messages if m["role"] == "tool")
    assert "exit=1" in tool_msg["content"]
    assert "command failed" in tool_msg["content"]


# --------------------------------------------------------------- malformed history


def test_pre_fix_bullet_format_raises_loudly(tmp_path: Path) -> None:
    """A history file written in the pre-2026-05-11 format
    (bullet with parenthesised args, no JSON fence) must
    raise on load rather than silently substituting a
    placeholder. The placeholder was the bug; failing
    loudly is the intended post-fix behaviour. Operator
    clears the history file and moves on."""
    store = _make_store(tmp_path)
    path = store.history_path("main")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "## 2026-05-07T10:00:00Z user\n\n"
        "hi\n\n"
        "## 2026-05-07T10:00:05Z assistant tool_calls\n\n"
        "- read_file(path='x.txt')\n\n"
        "## 2026-05-07T10:00:06Z tool read_file\n\n"
        "ok\n\n"
        "## 2026-05-07T10:00:07Z assistant\n\n"
        "done\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="malformed assistant tool_calls"):
        store.load_context("main")


def test_json_fence_without_closing_raises(tmp_path: Path) -> None:
    """A ```json fence that never closes is corruption;
    raise rather than silently eating the rest of the
    file."""
    store = _make_store(tmp_path)
    path = store.history_path("main")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "## 2026-05-07T10:00:00Z user\n\n"
        "hi\n\n"
        "## 2026-05-07T10:00:05Z assistant tool_calls\n\n"
        "- read_file\n"
        "```json\n"
        '{"path": "x.txt"\n'
        "\n"
        "## 2026-05-07T10:00:06Z tool read_file\n\n"
        "ok\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"never closed|not valid JSON"):
        store.load_context("main")


def test_json_fence_with_non_object_raises(tmp_path: Path) -> None:
    """OpenAI's tool_calls contract requires arguments to
    be a JSON object. A fenced array or scalar is malformed."""
    store = _make_store(tmp_path)
    path = store.history_path("main")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "## 2026-05-07T10:00:00Z user\n\n"
        "hi\n\n"
        "## 2026-05-07T10:00:05Z assistant tool_calls\n\n"
        "- read_file\n"
        "```json\n"
        '["not", "an", "object"]\n'
        "```\n\n"
        "## 2026-05-07T10:00:06Z tool read_file\n\n"
        "ok\n"
        "## 2026-05-07T10:00:07Z assistant\n\n"
        "done\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must be a JSON object"):
        store.load_context("main")


# --------------------------------------------------------------- back-compat (chat-only path)


def test_phase2_shape_file_loads_identically(tmp_path: Path) -> None:
    """A history file with only user + assistant turns (no
    tool-call machinery) loads identically across Phase 2
    and 5. The chat-only path stays back-compat; only the
    tool-using shape changed."""
    store = _make_store(tmp_path)
    path = store.history_path("main")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "## 2026-05-07T10:00:00Z user\n\n"
        "hi\n\n"
        "## 2026-05-07T10:00:05Z assistant\n\n"
        "hello there\n\n"
        "## 2026-05-07T10:10:00Z user\n\n"
        "bye\n\n"
        "## 2026-05-07T10:10:05Z assistant\n\n"
        "see you\n",
        encoding="utf-8",
    )
    ctx = store.load_context("main")
    assert ctx.history_messages == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello there"},
        {"role": "user", "content": "bye"},
        {"role": "assistant", "content": "see you"},
    ]


def test_unknown_role_header_handled_gracefully(
    tmp_path: Path,
) -> None:
    """A future schema might add new roles. The parser's
    pre-Phase-5 behaviour is "any line that doesn't match a
    header regex becomes content in the current turn" — so an
    unknown header bleeds into the preceding turn's body but
    doesn't crash the load. Following valid turns still parse.

    This is intentionally permissive. Strictly rejecting
    unknown headers would force a hard schema bump on every
    future evolution; swallowing them means newer-schema
    files still give useful output in older gateways.
    """
    store = _make_store(tmp_path)
    path = store.history_path("main")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "## 2026-05-07T10:00:00Z user\n\n"
        "hi\n\n"
        "## 2026-05-07T10:00:05Z assistant\n\n"
        "hello there\n\n"
        "## 2026-05-07T10:05:00Z future_kind\n\n"
        "newer schema content\n\n"
        "## 2026-05-07T10:10:00Z assistant\n\n"
        "after the unknown\n",
        encoding="utf-8",
    )
    ctx = store.load_context("main")
    texts = [m["content"] for m in ctx.history_messages]
    # The user turn survives clean.
    assert "hi" in texts
    # An assistant turn that includes "hello there" is present
    # (may also include the swallowed unknown-header block).
    assert any("hello there" in t for t in texts)
    # The follow-up assistant turn survives.
    assert any("after the unknown" in t for t in texts)
    # Minimum useful shape: user + at least one assistant.
    roles = [m["role"] for m in ctx.history_messages]
    assert "user" in roles
    assert "assistant" in roles


def test_orphan_tool_block_without_preceding_calls_skipped(
    tmp_path: Path,
) -> None:
    """A malformed file with a ``tool <name>`` block but no
    preceding ``assistant tool_calls`` — drop the orphan,
    don't blow up."""
    store = _make_store(tmp_path)
    path = store.history_path("main")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "## 2026-05-07T10:00:00Z user\n\n"
        "hi\n\n"
        "## 2026-05-07T10:00:05Z tool read_file\n\n"
        "orphaned content\n\n"
        "## 2026-05-07T10:00:10Z assistant\n\n"
        "hello\n",
        encoding="utf-8",
    )
    ctx = store.load_context("main")
    roles = [m["role"] for m in ctx.history_messages]
    # user + assistant; the tool block was orphan so it's
    # dropped with a debug log.
    assert roles == ["user", "assistant"]
