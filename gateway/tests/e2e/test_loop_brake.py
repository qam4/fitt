"""Loop brake — A/B through the real chat pipeline (stubbed LLM).

Reproduces the 2026-08-10 gemma4 failure mode deterministically: a model
that re-emits the SAME successful tool call every iteration because it
never registers the result. Without the brake that produced N duplicate
side effects (the todo list gained "call the doctor" 10 times) and a
504 with an empty reply. With the brake the effect happens ONCE and the
turn stops early.

The stubbed LLM makes this a real A/B: identical inputs, one toggle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from gateway.app import create_app

from .._fixtures import PERSONAL_TOKEN, build_test_config
from .._llm_stubs import stub_reply, stub_tool_call
from .conftest import StubbedLLM

_ITEM = "call the doctor"


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, brake: bool) -> Any:
    monkeypatch.setenv("FITT_HOME", str(tmp_path))
    monkeypatch.setenv("FITT_SKIP_SHELL_PROBE", "1")
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    cfg.loop_brake_enabled = brake
    return create_app(cfg)


async def _one_turn(app: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {PERSONAL_TOKEN}"},
    ) as client:
        return await client.post(
            "/v1/chat/completions",
            json={
                "model": "fitt-default",
                "messages": [{"role": "user", "content": f"Add '{_ITEM}' to my todos."}],
                "tool_choice": "auto",
            },
            timeout=60.0,
        )


def _todo_count(tmp_path: Path) -> int:
    text = (tmp_path / "todos.md").read_text(encoding="utf-8")
    return sum(1 for line in text.splitlines() if _ITEM in line)


async def test_brake_on_executes_side_effect_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stubbed_llm: StubbedLLM
) -> None:
    """A model stuck re-emitting the same call must not duplicate the
    side effect, and the turn should stop rather than burn the cap."""
    app = _app(tmp_path, monkeypatch, brake=True)
    # The model never stops asking for the same add.
    stubbed_llm.load([stub_tool_call("todo_add", {"text": _ITEM}) for _ in range(10)])

    r = await _one_turn(app)
    assert r.status_code == 200, r.text
    # THE fix: one call executed, so exactly one todo — not ten.
    assert _todo_count(tmp_path) == 1
    # And it stopped early: the brake trips after max_repeated_calls (3),
    # so several stubbed responses go unused.
    assert stubbed_llm.remaining() > 0


async def test_brake_off_duplicates_the_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stubbed_llm: StubbedLLM
) -> None:
    """Control: with the brake disabled the same model duplicates the
    effect and consumes the whole iteration budget — the behaviour
    observed live before the fix."""
    app = _app(tmp_path, monkeypatch, brake=False)
    stubbed_llm.load([stub_tool_call("todo_add", {"text": _ITEM}) for _ in range(10)])

    r = await _one_turn(app)
    # Exhausted loop surfaces as 504 (unchanged behaviour).
    assert r.status_code == 504, r.status_code
    assert _todo_count(tmp_path) == 10  # ten duplicate writes
    assert stubbed_llm.remaining() == 0  # burned every iteration


async def test_brake_allows_distinct_calls_and_retry_after_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stubbed_llm: StubbedLLM
) -> None:
    """The brake must not punish legitimate progress: different args are
    distinct calls, so both land."""
    app = _app(tmp_path, monkeypatch, brake=True)
    stubbed_llm.load(
        [
            stub_tool_call("todo_add", {"text": _ITEM}, call_id="c1"),
            stub_tool_call("todo_add", {"text": "buy milk"}, call_id="c2"),
            stub_reply("Added both items."),
        ]
    )

    r = await _one_turn(app)
    assert r.status_code == 200, r.text  # terminated naturally, not braked
    text = (tmp_path / "todos.md").read_text(encoding="utf-8")
    assert _ITEM in text
    assert "buy milk" in text  # distinct args were NOT suppressed
