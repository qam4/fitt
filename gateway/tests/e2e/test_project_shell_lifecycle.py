"""Phase 4.7 Task 9 — ``project_shell`` end-to-end.

Two scenarios:

* **Approve path.** Stubbed LLM emits a ``project_shell`` call;
  approver approves; backend runs the fake shell; the model
  wraps up with a reply. Assert the HTTP response is OK and
  a single ``tool_executed`` event with the right metadata
  lands in ``/v1/events``.

* **Reject path.** Approver rejects. Assert no
  ``tool_executed`` event (audit log has the rejection —
  event stream stays matched to "things that actually
  happened on the box"). The chat handler returns normally
  with the model's final reply.

The e2e fixtures default the shell probe off for speed; here we
inject a pre-resolved ``ShellInterpreter`` onto ``app.state`` so
``project_shell`` can dispatch. And we swap the real
``ExecutionBackend`` for a fake that doesn't actually spawn
subprocesses — the test is about the pipeline shape, not about
whether ``bash -lc`` runs on the dev laptop.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from gateway.tools.backend import ShellResult
from gateway.tools.local_shell import ShellInterpreter

from .._llm_stubs import stub_reply, stub_tool_call
from .conftest import E2EApprover, StubbedLLM, fetch_events, wait_for_event


class _FakeBackend:
    """In-process stand-in for the e2e harness.

    Records each ``run_shell`` call; pops pre-queued
    :class:`ShellResult` instances per call. Matches the real
    backend's signature closely enough that the tool's _impl
    doesn't notice."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._queue: list[ShellResult] = []

    def queue(self, result: ShellResult) -> None:
        self._queue.append(result)

    async def run_shell(
        self,
        project: Any,
        cmd: list[str],
        *,
        timeout_secs: int,
        **_: Any,
    ) -> ShellResult:
        self.calls.append(
            {
                "project": project.name,
                "ssh_host": project.ssh_host,
                "cmd": list(cmd),
                "timeout_secs": timeout_secs,
            }
        )
        if not self._queue:
            return ShellResult(exit=0, stdout="", stderr="", timed_out=False)
        return self._queue.pop(0)


@pytest.fixture
def project_shell_ready(e2e_app: Any) -> _FakeBackend:
    """Attach a fake ExecutionBackend + a bash interpreter so
    ``project_shell`` dispatches through this test's harness.

    The e2e default is ``FITT_SKIP_SHELL_PROBE=1`` with
    ``ShellInterpreter.none()``; project_shell's local path
    needs a real interpreter to dispatch, so we override here.

    Also disables the detach threshold — the shared e2e fixture
    sets it to 50ms for the detach-lifecycle tests, but these
    tests want the approval to resolve synchronously.
    """
    fake = _FakeBackend()
    e2e_app.state.execution_backend = fake
    e2e_app.state.local_shell = ShellInterpreter(
        label="bash",
        argv_prefix=("bash", "-lc"),
        available=True,
    )
    # Disable detach for these tests. Setting threshold above
    # the timeout makes the middleware never detach, so the
    # approver's decision resolves the chat turn synchronously.
    # (The detach-lifecycle test covers the detach path on
    # its own.)
    from gateway.tools import ToolPolicy

    policy = e2e_app.state.tool_registry.policy
    policy.approval_timeout_secs = 30.0
    policy.approval_detach_threshold_secs = 60.0
    # Approval middleware reads its timeout at construction;
    # refresh the timeout on the existing instance.
    e2e_app.state.approval._timeout_s = 30.0
    _ = ToolPolicy  # silence unused-import if optimiser trims
    return fake


@pytest.fixture
def telegram_approver(e2e_client: httpx.AsyncClient) -> E2EApprover:
    """Approver tagged as ``telegram`` so it sees approvals the
    test's telegram-tagged chat requests create. The default
    ``e2e_approver`` polls ``?client=webui`` and wouldn't see
    them."""
    return E2EApprover(e2e_client, client_tag="telegram")


# --------------------------------------------------------------- approve


async def test_approved_project_shell_emits_tool_executed(
    e2e_app: Any,
    e2e_client: httpx.AsyncClient,
    telegram_approver: E2EApprover,
    stubbed_llm: StubbedLLM,
    project_shell_ready: _FakeBackend,
) -> None:
    """Full happy path: approve → run → tool_executed event."""
    project_shell_ready.queue(
        ShellResult(
            exit=0,
            stdout="On branch main\nnothing to commit\n",
            stderr="",
            timed_out=False,
        )
    )

    stubbed_llm.load(
        [
            stub_tool_call(
                "project_shell",
                {"project": "hub", "command": "git status"},
            ),
            stub_reply("Your working tree is clean."),
        ]
    )

    async with telegram_approver.start(lambda p: "approve"):
        r = await e2e_client.post(
            "/v1/chat/completions",
            headers={"X-FITT-Client": "telegram"},
            json={
                "model": "fitt-default",
                "messages": [{"role": "user", "content": "what's the git status?"}],
                "tool_choice": "auto",
            },
        )
    assert r.status_code == 200, r.text
    reply = r.json()["choices"][0]["message"]["content"]
    assert "working tree" in reply.lower()

    # Backend was invoked once with the expected argv.
    assert len(project_shell_ready.calls) == 1
    call = project_shell_ready.calls[0]
    assert call["project"] == "hub"
    assert call["cmd"] == ["bash", "-lc", "git status"]

    # tool_executed event landed with the expected metadata.
    evt = await wait_for_event(e2e_client, kind="tool_executed", timeout_s=3.0)
    assert evt["meta"]["tool"] == "project_shell"
    assert evt["meta"]["project"] == "hub"
    assert evt["meta"]["command"] == "git status"
    assert evt["meta"]["exit_code"] == 0
    assert evt["meta"]["timed_out"] is False

    # Exactly one such event (the 2026-05-07 duplicate-push
    # bug inspired pinning this for every event-emitting path).
    all_events = await fetch_events(e2e_client, kind="tool_executed", limit=100)
    assert len(all_events) == 1


# --------------------------------------------------------------- reject


async def test_rejected_project_shell_emits_no_tool_executed(
    e2e_app: Any,
    e2e_client: httpx.AsyncClient,
    telegram_approver: E2EApprover,
    stubbed_llm: StubbedLLM,
    project_shell_ready: _FakeBackend,
) -> None:
    """Reject path: backend never runs, no tool_executed event.

    This is the "events mirror execution, not intent" invariant
    from the design doc. An operator scanning ``fitt inbox``
    should never see a ``tool_executed`` for a command that
    didn't actually execute — that would mislead.
    """
    stubbed_llm.load(
        [
            stub_tool_call(
                "project_shell",
                {"project": "hub", "command": "rm -rf some-sensitive-thing"},
            ),
            stub_reply("Understood. Standing down."),
        ]
    )

    async with telegram_approver.start(lambda p: "reject"):
        r = await e2e_client.post(
            "/v1/chat/completions",
            headers={"X-FITT-Client": "telegram"},
            json={
                "model": "fitt-default",
                "messages": [{"role": "user", "content": "delete that thing"}],
                "tool_choice": "auto",
            },
        )
    assert r.status_code == 200, r.text
    reply = r.json()["choices"][0]["message"]["content"]
    assert "standing down" in reply.lower()

    # Backend was NOT invoked.
    assert project_shell_ready.calls == []

    # No tool_executed event in the log.
    events = await fetch_events(e2e_client, kind="tool_executed", limit=100)
    assert events == [], (
        "rejected project_shell must not produce a tool_executed "
        "event — audit log has the rejection; the event stream "
        "stays matched to 'things that actually happened.'"
    )


# --------------------------------------------------------------- deny list


async def test_deny_listed_command_blocks_without_event(
    e2e_app: Any,
    e2e_client: httpx.AsyncClient,
    telegram_approver: E2EApprover,
    stubbed_llm: StubbedLLM,
    project_shell_ready: _FakeBackend,
) -> None:
    """Deny-list-blocked command: approval middleware rejects
    BEFORE bucket resolution, so the approver never sees
    anything to decide. Backend never runs. No tool_executed
    event.

    Note: the approver is configured to ``None`` (skip
    everything) — if the deny list fails and the call somehow
    reaches the approval prompt, the test times out on the
    approver rather than silently proceeding."""
    stubbed_llm.load(
        [
            stub_tool_call(
                "project_shell",
                {"project": "hub", "command": "rm -rf $FITT_HOME"},
            ),
            stub_reply("Operation refused; standing down."),
        ]
    )

    # Policy returns None for everything — the deny list should
    # prevent any approval from reaching this.
    async with telegram_approver.start(lambda p: None):
        r = await e2e_client.post(
            "/v1/chat/completions",
            headers={"X-FITT-Client": "telegram"},
            json={
                "model": "fitt-default",
                "messages": [{"role": "user", "content": "nuke my config"}],
                "tool_choice": "auto",
            },
        )
    assert r.status_code == 200, r.text

    assert project_shell_ready.calls == []
    events = await fetch_events(e2e_client, kind="tool_executed", limit=100)
    assert events == []
