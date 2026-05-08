"""Tests for Phase 4.5 Task 5.5 — detached delivery.

Exercises the full detach lifecycle: the HTTP handler receives a
placeholder response within the detach threshold, the background
worker finishes the tool loop once the approval is resolved, and a
``late_tool_result`` (or ``late_tool_rejected``) event lands in
the log with the turn appended to memory.

Three invariants under test (mirroring the design doc):

* **Synchronous placeholder.** The HTTP client unblocks inside
  ``approval_detach_threshold_secs`` — not after the middleware's
  full approval-timeout.
* **Asynchronous event.** Once the approval resolves, the late
  event kind matches the user's choice (approve → result, reject
  → rejected) and carries the final reply as body.
* **No-push-channel warning.** When no Telegram bot is
  configured, the detached worker logs a WARNING at completion
  so the operator sees that the event is only visible via
  ``fitt inbox``.

Tests use an in-process ASGI transport so the HTTP request, the
approval's Future, and the background detached worker all share
one event loop. ``TestClient`` would spawn a separate thread and
loop per call, which breaks the Future-bound-to-loop invariant
the approval middleware relies on.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from gateway.app import create_app
from gateway.config import Secrets, TelegramSecrets
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
) -> Any:
    """Compat shim → shared ``make_response``."""
    return make_response(content=content, tool_calls=tool_calls)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


def _build_app(
    tmp_path: Path,
    *,
    detach_threshold_s: float | None,
    approval_timeout_s: float = 10.0,
    with_telegram: bool = False,
) -> Any:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.tools = {"approval_timeout_secs": approval_timeout_s}
    if detach_threshold_s is not None:
        cfg.tools["approval_detach_threshold_secs"] = detach_threshold_s
    if with_telegram:
        assert cfg.secrets is not None
        cfg.secrets = Secrets(
            allowed_tokens=list(cfg.secrets.allowed_tokens),
            openrouter_api_key=cfg.secrets.openrouter_api_key,
            telegram=TelegramSecrets(bot_token="t", allowlist_user_ids=[1]),
        )
    app = create_app(cfg)

    # A project for the tool to target. We never actually run the
    # tool (the approval is awaiting the test's decision), but
    # the ToolContext needs a valid project to resolve against.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    app.state.project_registry.add(
        Project(name="hub", ssh_host="", path=str(repo), test_command="pytest -q")
    )
    return app


def _asgi_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


async def _wait_for_pending(approval: Any, *, timeout_s: float = 2.0) -> Any:
    """Poll the middleware until a pending approval exists."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        p = await approval.list_pending()
        if p:
            return p[0]
        await asyncio.sleep(0.01)
    raise AssertionError("no pending approval within timeout")


async def _wait_for_event(events: Any, *, kind: str, timeout_s: float = 3.0) -> Any:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        found = [e for e in events.read() if e.kind == kind]
        if found:
            return found[-1]
        await asyncio.sleep(0.01)
    raise AssertionError(f"no {kind!r} event within {timeout_s}s")


async def _drain_approvals(approval: Any) -> None:
    """Reject anything still pending so the test tear-down doesn't
    leak never-awaited futures."""
    pending = await approval.list_pending()
    for p in pending:
        await approval.resolve_approval(p.approval_id, "reject")


# --------------------------------------------------------------- synchronous placeholder


async def test_detach_returns_placeholder_synchronously(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the detach threshold at 50ms and the approval still
    pending at that mark, the HTTP client sees the ⏳ placeholder
    body rather than waiting out the middleware's 10s timeout."""

    async def fake(**_: Any) -> Any:
        return _fake_response(
            tool_calls=[
                _tool_call(
                    "c1",
                    "write_file",
                    {"project": "hub", "path": "a", "content": "x"},
                )
            ]
        )

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)
    app = _build_app(tmp_path, detach_threshold_s=0.05)
    try:
        async with _asgi_client(app) as http:
            r = await http.post(
                "/v1/chat/completions",
                json={
                    "model": "fitt-smart",
                    "messages": [{"role": "user", "content": "write the file"}],
                    "tool_choice": "auto",
                },
                headers=_auth(),
            )
        assert r.status_code == 200, r.text
        body = r.json()
        content = body["choices"][0]["message"]["content"]
        assert content.startswith("⏳ Approval pending")
        assert r.headers.get("X-FITT-Detached") == "1"
    finally:
        # Let the detached worker (still awaiting the approval) resolve.
        await _drain_approvals(app.state.approval)
        # Give the worker a tick to finish so pytest's warnings
        # capture stays clean.
        await asyncio.sleep(0.05)


# --------------------------------------------------------------- full lifecycle


async def test_detach_event_lands_after_approve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detach, resolve approval as 'approve', the detached worker
    completes the second dispatch, emits late_tool_result, and
    appends the turn to memory."""
    calls: list[int] = []

    async def fake(**_: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            return _fake_response(
                tool_calls=[
                    _tool_call(
                        "c1",
                        "write_file",
                        {"project": "hub", "path": "a.txt", "content": "x"},
                    )
                ]
            )
        return _fake_response(content="wrote a.txt. done.")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)
    app = _build_app(tmp_path, detach_threshold_s=0.05)

    async with _asgi_client(app) as http:
        r = await http.post(
            "/v1/chat/completions",
            json={
                "model": "fitt-smart",
                "messages": [{"role": "user", "content": "write a.txt"}],
                "tool_choice": "auto",
            },
            headers=_auth(),
        )
        assert r.status_code == 200
        assert r.headers.get("X-FITT-Detached") == "1"

        approval = app.state.approval
        pending = await _wait_for_pending(approval)
        ok = await approval.resolve_approval(pending.approval_id, "approve")
        assert ok

        evt = await _wait_for_event(app.state.events, kind="late_tool_result")
        assert "wrote a.txt" in evt.body
        assert evt.meta["tool"] == "write_file"
        assert evt.meta["approval_id"] == pending.approval_id
        # The _fixtures.PERSONAL_TOKEN has no client tag so auth
        # normalises it to "webui" — the detach worker records
        # whatever the auth middleware saw.
        assert evt.meta["original_client"] == "webui"

        # Memory should have gained the turn. The sessions dir for
        # "main" ought to have a history file with "wrote a.txt"
        # inside it.
        history_dir = app.state.config.memory.sessions_dir / "main" / "history"
        assert history_dir.exists()
        entries = list(history_dir.glob("*.md"))
        assert entries, "expected a history file for session=main"
        combined = "\n".join(p.read_text(encoding="utf-8") for p in entries)
        assert "wrote a.txt" in combined


async def test_detach_event_lands_after_reject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User rejects the detached approval → late_tool_rejected,
    the second dispatch sees the tool-rejection error in the
    tool-result message and the model wraps up."""
    calls: list[int] = []

    async def fake(**_: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            return _fake_response(
                tool_calls=[
                    _tool_call(
                        "c1",
                        "write_file",
                        {"project": "hub", "path": "a.txt", "content": "x"},
                    )
                ]
            )
        return _fake_response(content="user rejected. backing off.")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)
    app = _build_app(tmp_path, detach_threshold_s=0.05)

    async with _asgi_client(app) as http:
        r = await http.post(
            "/v1/chat/completions",
            json={
                "model": "fitt-smart",
                "messages": [{"role": "user", "content": "write"}],
                "tool_choice": "auto",
            },
            headers=_auth(),
        )
        assert r.status_code == 200
        assert r.headers.get("X-FITT-Detached") == "1"

        approval = app.state.approval
        pending = await _wait_for_pending(approval)
        await approval.resolve_approval(pending.approval_id, "reject")

        evt = await _wait_for_event(app.state.events, kind="late_tool_rejected")
        assert "rejected" in evt.body.lower()


# --------------------------------------------------------------- no-detach path


async def test_no_detach_threshold_means_synchronous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``approval_detach_threshold_secs`` in config, a
    natural-stop response goes down the synchronous path — no
    placeholder, no ``X-FITT-Detached`` header."""

    async def fake(**_: Any) -> Any:
        return _fake_response(content="plain reply")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)
    app = _build_app(tmp_path, detach_threshold_s=None)

    async with _asgi_client(app) as http:
        r = await http.post(
            "/v1/chat/completions",
            json={
                "model": "fitt-smart",
                "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": "auto",
            },
            headers=_auth(),
        )
    assert r.status_code == 200
    body = r.json()
    assert "⏳" not in body["choices"][0]["message"]["content"]
    assert "X-FITT-Detached" not in r.headers


# --------------------------------------------------------------- no-push-channel warning


async def test_detach_without_telegram_logs_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Task 5.5d: when no push channel is configured, the detach
    path still works (event lands, memory updates) and a WARNING
    fires so the operator sees that the event is ``fitt inbox``-
    only."""
    calls: list[int] = []

    async def fake(**_: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            return _fake_response(
                tool_calls=[
                    _tool_call(
                        "c1",
                        "write_file",
                        {"project": "hub", "path": "a", "content": "x"},
                    )
                ]
            )
        return _fake_response(content="got the rejection, standing down.")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)
    app = _build_app(tmp_path, detach_threshold_s=0.05, with_telegram=False)

    with caplog.at_level(logging.WARNING):
        async with _asgi_client(app) as http:
            r = await http.post(
                "/v1/chat/completions",
                json={
                    "model": "fitt-smart",
                    "messages": [{"role": "user", "content": "write"}],
                    "tool_choice": "auto",
                },
                headers=_auth(),
            )
            assert r.status_code == 200
            assert r.headers.get("X-FITT-Detached") == "1"

            approval = app.state.approval
            pending = await _wait_for_pending(approval)
            await approval.resolve_approval(pending.approval_id, "reject")
            await _wait_for_event(app.state.events, kind="late_tool_rejected")

    messages = [r.getMessage() for r in caplog.records]
    assert any("no_push_channel" in m for m in messages), messages


async def test_detach_with_telegram_skips_no_push_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mirror test: with Telegram configured, the detach-time
    no-push-channel warning should NOT fire. Everything else
    (event, memory) still lands."""
    calls: list[int] = []

    async def fake(**_: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            return _fake_response(
                tool_calls=[
                    _tool_call(
                        "c1",
                        "write_file",
                        {"project": "hub", "path": "a", "content": "x"},
                    )
                ]
            )
        return _fake_response(content="got the rejection, standing down.")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)
    app = _build_app(tmp_path, detach_threshold_s=0.05, with_telegram=True)

    with caplog.at_level(logging.WARNING):
        async with _asgi_client(app) as http:
            r = await http.post(
                "/v1/chat/completions",
                json={
                    "model": "fitt-smart",
                    "messages": [{"role": "user", "content": "write"}],
                    "tool_choice": "auto",
                },
                headers=_auth(),
            )
            assert r.status_code == 200

            approval = app.state.approval
            pending = await _wait_for_pending(approval)
            await approval.resolve_approval(pending.approval_id, "reject")
            await _wait_for_event(app.state.events, kind="late_tool_rejected")

    messages = [r.getMessage() for r in caplog.records]
    assert not any("no_push_channel" in m for m in messages), messages
