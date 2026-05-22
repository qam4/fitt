"""Tests for :mod:`gateway.turn_capture` — Phase 7 Slice 7.2.

Three concerns:

* Write path: atomic (rename, no half-files), non-blocking
  (returns immediately), per-day directory layout.
* Read path: by turn_id (any day), list_recent (newest-first,
  limit + since filtering).
* Privacy default: ``coding-agent`` skips by default;
  operator override via ``default_capture`` config flips
  it on.

The store never raises. Tests assert on returned values
(``None`` from a failed write) and on log messages, not on
propagated exceptions.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.turn_capture import (
    CapturedToolCall,
    TurnCapture,
    TurnCaptureBuilder,
    TurnCaptureStore,
    should_capture,
)

# --------------------------------------------------------------- helpers


def _make_capture(
    *,
    turn_id: str = "turn-1",
    session_key: str = "main",
    started_at: float = 1779479823.42,
    finished_at: float = 1779479825.81,
    prompt_tokens: int = 5400,
    context_window: int | None = 32768,
    tool_calls: list[CapturedToolCall] | None = None,
    client: str = "telegram",
    narration_warning: bool = False,
    status: str = "ok",
) -> TurnCapture:
    return TurnCapture(
        turn_id=turn_id,
        session_key=session_key,
        alias="fitt-default",
        client=client,
        model_used="qwen2.5-coder:14b",
        backend="ollama",
        fallback_used=False,
        started_at=started_at,
        finished_at=finished_at,
        dispatched_messages=[
            {"role": "system", "content": "You are FITT..."},
            {"role": "user", "content": "Read README.md"},
        ],
        response={
            "choices": [
                {
                    "message": {"role": "assistant", "content": "..."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 89},
        },
        tool_calls=tool_calls or [],
        prompt_tokens=prompt_tokens,
        completion_tokens=89,
        context_window=context_window,
        prompt_pct_of_window=((prompt_tokens / context_window * 100) if context_window else None),
        finish_reason="stop",
        narration_warning=narration_warning,
        iterations=1,
        status=status,
    )


# --------------------------------------------------------------- write


def test_write_creates_per_day_directory(tmp_path: Path) -> None:
    """Capture lands at sessions/<key>/turns/<YYYY-MM-DD>/<turn_id>.json
    so the dashboard's date-bucketed listing works."""
    store = TurnCaptureStore(sessions_dir=tmp_path)
    capture = _make_capture(turn_id="turn-A")
    path = store.write(capture)
    assert path is not None
    expected_day = datetime.fromtimestamp(capture.finished_at, tz=UTC).strftime("%Y-%m-%d")
    assert path == tmp_path / "main" / "turns" / expected_day / "turn-A.json"
    assert path.exists()


def test_write_payload_round_trips_through_read(tmp_path: Path) -> None:
    """The dataclass written and the dataclass read back must
    match field-for-field. Programmer-grade traceability fails
    if the on-disk format is lossy."""
    store = TurnCaptureStore(sessions_dir=tmp_path)
    capture = _make_capture(
        tool_calls=[
            CapturedToolCall(
                call_id="c1",
                tool_name="read_file",
                args={"project": "fitt", "path": "README.md"},
                decision="auto",
                decision_detail="",
                duration_ms=12,
                ok=True,
                result_summary="(file content)",
                artifact_path=None,
                iteration=0,
            )
        ],
    )
    store.write(capture)
    got = store.read("main", "turn-1")
    assert got is not None
    assert got.turn_id == capture.turn_id
    assert got.dispatched_messages == capture.dispatched_messages
    assert got.tool_calls[0].args == {"project": "fitt", "path": "README.md"}
    assert got.prompt_tokens == 5400
    assert got.context_window == 32768
    assert got.prompt_pct_of_window == pytest.approx(16.479, abs=0.01)


def test_write_atomic_via_tmp_rename(tmp_path: Path) -> None:
    """The write goes to ``<turn_id>.json.<rand>.tmp`` first
    and renames. Half-written files don't appear under the
    canonical name."""
    store = TurnCaptureStore(sessions_dir=tmp_path)
    capture = _make_capture(turn_id="turn-atomic")

    seen_paths: list[Path] = []
    real_replace = os.replace

    def _capture_replace(src: object, dst: object) -> None:
        seen_paths.append(Path(str(src)))
        real_replace(src, dst)  # type: ignore[arg-type]

    with patch("gateway.turn_capture.os.replace", side_effect=_capture_replace):
        store.write(capture)

    # Source path of the rename is a tmp file matching the
    # turn id.
    assert len(seen_paths) == 1
    assert seen_paths[0].name.startswith("turn-atomic.json.")
    assert seen_paths[0].name.endswith(".tmp")


def test_write_failure_returns_none_and_no_canonical_file(tmp_path: Path) -> None:
    """When the write raises (full disk, permission error),
    the store logs WARNING and returns None. No canonical file
    appears."""
    # Make the sessions_dir read-only after creation so the
    # mkdir for the day directory fails.
    sessions_dir = tmp_path / "ro"
    sessions_dir.mkdir()
    store = TurnCaptureStore(sessions_dir=sessions_dir)

    # Simulate the write_text failure without depending on
    # filesystem permissions which differ across platforms.
    with patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
        result = store.write(_make_capture())

    assert result is None
    # No canonical file landed.
    canonical_glob = list(sessions_dir.rglob("*.json"))
    assert canonical_glob == []


def test_write_async_returns_task(tmp_path: Path) -> None:
    """The async wrapper returns an awaitable Task. The chat
    handler uses fire-and-forget — it doesn't await — but
    tests can to confirm completion."""
    store = TurnCaptureStore(sessions_dir=tmp_path)

    async def run() -> Path | None:
        task = store.write_async(_make_capture(turn_id="turn-async"))
        assert isinstance(task, asyncio.Task)
        return await task

    result = asyncio.run(run())
    assert result is not None
    assert result.exists()


# --------------------------------------------------------------- read


def test_read_returns_none_for_unknown_turn(tmp_path: Path) -> None:
    """Unknown turn_id → None. The endpoint translates to
    404."""
    store = TurnCaptureStore(sessions_dir=tmp_path)
    assert store.read("main", "nonexistent") is None


def test_read_finds_turn_in_any_day_directory(tmp_path: Path) -> None:
    """Lookup by turn_id walks all per-day directories under
    the session. Capture days don't necessarily match call
    days (clock drift, retention edge), so a search is more
    robust than computing a single day."""
    store = TurnCaptureStore(sessions_dir=tmp_path)
    # Two captures on different days.
    older = _make_capture(turn_id="turn-old", finished_at=1700000000.0)
    newer = _make_capture(turn_id="turn-new", finished_at=1779479825.81)
    store.write(older)
    store.write(newer)

    assert store.read("main", "turn-old") is not None
    assert store.read("main", "turn-new") is not None


def test_list_recent_returns_summary_without_bodies(tmp_path: Path) -> None:
    """list_recent returns lightweight dicts. Bodies stay on
    disk; the dashboard's list view loads in a sane time
    even with thousands of past turns."""
    store = TurnCaptureStore(sessions_dir=tmp_path)
    cap = _make_capture(turn_id="turn-1")
    store.write(cap)

    items = store.list_recent("main")
    assert len(items) == 1
    item = items[0]
    # Summary fields present.
    assert item["turn_id"] == "turn-1"
    assert item["model_used"] == "qwen2.5-coder:14b"
    assert item["prompt_tokens"] == 5400
    assert item["context_window"] == 32768
    # Bodies absent.
    assert "dispatched_messages" not in item
    assert "response" not in item
    assert "tool_calls" not in item
    assert item["tool_calls_count"] == 0


def test_list_recent_orders_newest_first(tmp_path: Path) -> None:
    """List view shows the most recent turn at the top —
    that's the one the operator usually wants."""
    store = TurnCaptureStore(sessions_dir=tmp_path)
    older = _make_capture(turn_id="turn-old", finished_at=1700000000.0)
    newer = _make_capture(turn_id="turn-new", finished_at=1779479825.81)
    store.write(older)
    store.write(newer)

    items = store.list_recent("main")
    assert [i["turn_id"] for i in items] == ["turn-new", "turn-old"]


def test_list_recent_respects_limit(tmp_path: Path) -> None:
    """``limit`` caps the number of turns returned."""
    store = TurnCaptureStore(sessions_dir=tmp_path)
    base_ts = 1779479825.81
    for i in range(5):
        store.write(_make_capture(turn_id=f"turn-{i}", finished_at=base_ts - i))

    items = store.list_recent("main", limit=3)
    assert len(items) == 3


def test_list_recent_filters_by_since(tmp_path: Path) -> None:
    """``since`` filters by ``started_at`` so the dashboard's
    'past 24 hours' view doesn't load every captured turn."""
    store = TurnCaptureStore(sessions_dir=tmp_path)
    old = _make_capture(turn_id="turn-old", started_at=1.0, finished_at=2.0)
    new = _make_capture(turn_id="turn-new", started_at=1779479825.0, finished_at=1779479826.0)
    store.write(old)
    store.write(new)

    items = store.list_recent("main", since=1779000000.0)
    assert [i["turn_id"] for i in items] == ["turn-new"]


def test_read_handles_corrupt_file_returns_none(tmp_path: Path) -> None:
    """Garbled JSON → log warning, return None. The endpoint
    treats the turn as missing rather than crashing."""
    store = TurnCaptureStore(sessions_dir=tmp_path)
    cap = _make_capture(turn_id="turn-corrupt")
    path = store.write(cap)
    assert path is not None

    path.write_text("{ not valid json", encoding="utf-8")
    assert store.read("main", "turn-corrupt") is None


def test_read_handles_shape_mismatch(tmp_path: Path) -> None:
    """JSON that parses but doesn't match the dataclass → None.
    A future schema change that lands a v2 capture must not
    crash the v1 reader."""
    store = TurnCaptureStore(sessions_dir=tmp_path)
    day_dir = store.turn_dir("main", datetime.fromtimestamp(1779479825.81, tz=UTC))
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "turn-future.json").write_text(
        json.dumps({"unknown_format": "v2"}),
        encoding="utf-8",
    )
    assert store.read("main", "turn-future") is None


# --------------------------------------------------------------- privacy gate


def test_should_capture_default_for_telegram() -> None:
    """Agent-mode clients capture by default."""
    assert should_capture(
        client="telegram",
        config_default=["telegram", "webui", "cli", "ide"],
    )


def test_should_capture_default_off_for_coding_agent() -> None:
    """Router-mode clients don't capture by default — the thin-
    router contract says FITT shouldn't persist their bodies."""
    assert not should_capture(
        client="coding-agent",
        config_default=["telegram", "webui", "cli", "ide"],
    )


def test_should_capture_operator_override_can_enable_coding_agent() -> None:
    """Operator can opt in via config — adds coding-agent to
    the default list."""
    assert should_capture(
        client="coding-agent",
        config_default=["telegram", "webui", "cli", "ide", "coding-agent"],
    )


def test_should_capture_master_disable_overrides_everything() -> None:
    """``traceability.enabled = false`` shuts capture down
    regardless of per-client default."""
    assert not should_capture(
        client="telegram",
        config_default=["telegram"],
        enabled=False,
    )


def test_should_capture_unknown_client_uses_router_mode_predicate() -> None:
    """A client tag we haven't seen before captures unless
    it's recognised as router-mode. ``ide`` not in the
    config_default list still captures because it's not
    coding-agent."""
    # 'ide' not in list, but is_router_mode_client('ide') is False.
    assert should_capture(client="ide", config_default=[])


# --------------------------------------------------------------- builder


def test_builder_assembles_capture_with_pct() -> None:
    """The builder centralises field assembly so the chat
    handler's wiring stays small. prompt_pct_of_window
    derives from prompt_tokens / context_window."""
    builder = TurnCaptureBuilder(
        turn_id="t",
        session_key="main",
        alias="fitt-default",
        client="telegram",
        started_at=1.0,
    )
    builder.dispatched_messages = [{"role": "user", "content": "hi"}]
    builder.response = {"choices": [{"message": {"content": "hello"}}]}
    builder.model_used = "qwen2.5-coder:14b"
    builder.backend = "ollama"
    builder.prompt_tokens = 5400
    builder.completion_tokens = 89
    builder.context_window = 32768

    cap = builder.build(finished_at=2.0)
    assert cap.prompt_pct_of_window == pytest.approx(16.479, abs=0.01)
    assert cap.finished_at == 2.0


def test_builder_handles_missing_context_window() -> None:
    """When discovery failed for the bound model,
    context_window is None and pct must also be None — the
    dashboard renders 'unknown' rather than 0%."""
    builder = TurnCaptureBuilder(
        turn_id="t",
        session_key="main",
        alias="fitt-default",
        client="telegram",
        started_at=1.0,
    )
    builder.prompt_tokens = 5400
    builder.context_window = None
    cap = builder.build()
    assert cap.context_window is None
    assert cap.prompt_pct_of_window is None
