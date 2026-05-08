"""Shared fixtures for the Phase 4.6 end-to-end harness.

Every fixture here is composable with the others: a test can
request any subset. See ``.kiro/specs/phase4.6-e2e-harness/design.md``
for the rationale ("Fixtures" and "Approver helper" sections).

Design highlights, pinned here so contributors don't have to chase
the spec to understand why things look weird:

* **We use ``httpx.AsyncClient(ASGITransport(app))``**, not
  ``TestClient``. The approval middleware stashes an
  ``asyncio.Future`` in a dict and awaits it; the decide handler
  sets it from the bot's POST. Both sides must share one event
  loop or the Future never wakes. ``TestClient`` spawns a thread
  and a separate loop per call — it breaks here. ``test_detach.py``
  discovered this the hard way.

* **Lifespan is NOT entered.** ``create_app`` registers
  ``@app.on_event("startup")`` for MCP and the cron scheduler.
  In tests we drive the scheduler manually via ``e2e_clock``,
  and MCP never runs (test config has no MCP servers).
  ``AsyncClient(ASGITransport(app))`` does not invoke lifespan
  by default, which is what we want.

* **Default client tag is ``webui``.** The test
  ``PERSONAL_TOKEN`` in ``tests/_fixtures.py`` has no
  ``client:`` field, so the auth middleware resolves it to
  ``webui`` (least-trusted default). Approvers default to
  ``client_tag="webui"`` to match. Tests that want a different
  identity can pass ``client_tag=...`` explicitly.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable, Iterable
from pathlib import Path
from typing import Any

import httpx
import pytest

from gateway.app import create_app
from gateway.projects import Project

from .._fixtures import PERSONAL_TOKEN, build_test_config
from .._llm_stubs import make_response  # re-exported for convenience

__all__ = [
    "E2EApprover",
    "E2EClock",
    "StubbedLLM",
    "make_response",
]

# --------------------------------------------------------------- clock


class E2EClock:
    """Explicit time control for the cron scheduler.

    ``advance`` mutates an internal virtual-now float;
    ``run_due`` calls ``scheduler.tick(now=self._now)`` once and
    awaits every in-flight firing task the tick spawned, so the
    next line of the test body can assert on events immediately.

    Does not control ``time.time()`` globally. Where the gateway
    calls ``time.time()`` directly (event timestamps, approval
    ``created_at``, ``CronService`` setting ``created_ts`` /
    ``last_run_ts``), tests tolerate real wall-clock. Only the
    scheduler's "is this job due" decision is test-controlled.

    The clock therefore defaults to ``time.time()`` at fixture
    construction so virtual-now stays close to the gateway's
    internal wall-clock. Advancing 120s from real-time means
    ``created_ts + 60 <= virtual_now`` holds, which is what
    :meth:`CronScheduler._is_due` checks. Override ``start`` for
    tests that want to pin an absolute virtual time.
    """

    def __init__(self, *, start: float | None = None) -> None:
        self._now = time.time() if start is None else start

    @property
    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the virtual clock forward by ``seconds``."""
        self._now += seconds

    async def run_due(self, scheduler: Any) -> list[str]:
        """Tick ``scheduler`` at the virtual-now and await every
        firing task it spawned. Returns the ids of jobs that
        started firing this tick.
        """
        fired = await scheduler.tick(now=self._now)
        # Wait on each in-flight firing so events + memory are
        # observable on the next line of the test body. Copy
        # defensively because _in_flight mutates as tasks complete.
        for task in list(scheduler._in_flight.values()):
            if not task.done():
                try:
                    await task
                except Exception:
                    # Failure paths emit cron_failed events; the
                    # test inspects them. Don't re-raise here or
                    # the test can't distinguish "firing failed
                    # as expected" from "harness blew up."
                    pass
        return fired


# --------------------------------------------------------------- approver


class E2EApprover:
    """Stand-in for the Telegram bot's approval poller.

    Two modes:

    * **Scripted** (``async with approver.start(policy):``)
      — a background task polls ``/v1/approvals/pending`` every
      10ms and applies ``policy`` to each pending approval.
      ``policy`` returns a decision string or ``None`` to skip.

    * **One-shot** (``await approver.wait_for(tool=...)`` then
      ``await approver.decide(id, decision)``) — for tests that
      want to hold an approval past the detach threshold before
      deciding. The detach lifecycle test uses this.

    The approver never calls ``resolve_approval`` on the
    middleware directly; it goes through the same HTTP surface
    the bot does, so the auth + client-tag path is exercised.
    """

    def __init__(self, client: httpx.AsyncClient, *, client_tag: str = "webui") -> None:
        self._client = client
        self._client_tag = client_tag

    # ------------------------------- low-level

    def _headers(self) -> dict[str, str]:
        """Ensure approver HTTP calls are tagged with its
        ``client_tag`` so the decide endpoint's client-match
        check accepts them. Without this an approver polling
        ``?client=telegram`` would still POST decisions
        tagged ``webui`` (untagged token default), and the
        gateway's 403 check on mismatched clients would fire."""
        return {"X-FITT-Client": self._client_tag}

    async def list_pending(self) -> list[dict[str, Any]]:
        r = await self._client.get(
            "/v1/approvals/pending",
            params={"client": self._client_tag},
            headers=self._headers(),
        )
        r.raise_for_status()
        data = r.json()
        return list(data.get("pending", []))

    async def decide(self, approval_id: str, decision: str) -> None:
        r = await self._client.post(
            f"/v1/approvals/{approval_id}/decide",
            json={"decision": decision},
            headers=self._headers(),
        )
        r.raise_for_status()

    # ------------------------------- one-shot

    async def wait_for(
        self,
        *,
        tool: str | None = None,
        timeout_s: float = 3.0,
    ) -> dict[str, Any]:
        """Poll until a pending approval matching ``tool`` appears.

        Returns the first matching entry as a dict. If ``tool`` is
        ``None``, returns the first pending approval of any kind.
        Raises ``AssertionError`` on timeout.
        """
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            pending = await self.list_pending()
            for p in pending:
                if tool is None or p.get("tool") == tool:
                    return p
            await asyncio.sleep(0.01)
        raise AssertionError(f"no pending approval for tool={tool!r} within {timeout_s}s")

    # ------------------------------- scripted

    @contextlib.asynccontextmanager
    async def start(
        self,
        policy: Callable[[dict[str, Any]], str | None],
    ) -> AsyncIterator[None]:
        """Run a background poller until the context exits.

        ``policy(pending_dict) -> "approve" | "reject" | "trust_session" | None``.
        Returning ``None`` skips this approval this pass — useful
        for "approve the second one, ignore the first until it
        times out."

        Decisions that come back from ``decide`` with an HTTP
        error (404 because the approval was already resolved)
        are swallowed — racy polling is fine here.
        """
        stop = asyncio.Event()
        # Track ids we've already decided so we don't re-POST on
        # the same approval. The gateway's list_pending stops
        # returning resolved approvals, but a racing decode could
        # see the same id twice.
        decided: set[str] = set()

        async def _loop() -> None:
            while not stop.is_set():
                try:
                    pending = await self.list_pending()
                except Exception:
                    # The app might not be ready yet on the first
                    # iteration. One tick and retry.
                    await asyncio.sleep(0.01)
                    continue
                for entry in pending:
                    approval_id = entry["id"]
                    if approval_id in decided:
                        continue
                    decision = policy(entry)
                    if decision is None:
                        continue
                    decided.add(approval_id)
                    try:
                        await self.decide(approval_id, decision)
                    except httpx.HTTPStatusError:
                        # Timed out on the gateway side between our
                        # list and our decide — nothing we can do.
                        pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.01)
                except TimeoutError:
                    continue

        task = asyncio.create_task(_loop(), name="e2e-approver-poller")
        try:
            yield
        finally:
            stop.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, timeout=1.0)
            if not task.done():
                task.cancel()
                with contextlib.suppress(Exception):
                    await task


# --------------------------------------------------------------- stubbed LLM


class StubbedLLM:
    """Reusable wrapper around ``_llm_stubs.stub_sequence``.

    ``.load(responses)`` replaces the queue (useful if a test
    drives multiple chat turns and wants to reset between
    them). ``.calls`` captures every dispatch's kwargs so tests
    can assert on which alias resolved, what system prompt was
    built, etc. Empty queue raises ``AssertionError`` rather
    than ``StopIteration`` — the failure site names the
    fixture so debugging is fast.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._queue: list[Any] = []

    def load(self, responses: Iterable[Any]) -> None:
        self._queue = list(responses)

    def extend(self, responses: Iterable[Any]) -> None:
        """Append to the queue without clearing — for multi-phase
        tests that add more responses mid-flight."""
        self._queue.extend(responses)

    def remaining(self) -> int:
        return len(self._queue)

    async def _dispatch(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._queue:
            raise AssertionError(
                "stubbed_llm: no more responses queued. Either the "
                "test's stub.load([...]) was too short, or the code "
                "under test dispatched more rounds than expected."
            )
        return self._queue.pop(0)


# --------------------------------------------------------------- helpers


async def wait_for_event(
    client: httpx.AsyncClient,
    *,
    kind: str,
    since: float | None = None,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    """Poll ``/v1/events`` until an event of ``kind`` lands.

    ``since`` is passed through so tests that already observed
    earlier events can filter cleanly. Raises ``AssertionError``
    on timeout — clear failure site for the test body.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get("/v1/events", params={"since": since} if since else {})
        r.raise_for_status()
        events = r.json().get("events", [])
        for e in events:
            if e["kind"] == kind:
                return e
        await asyncio.sleep(0.02)
    raise AssertionError(f"no {kind!r} event within {timeout_s}s")


async def fetch_events(
    client: httpx.AsyncClient,
    *,
    since: float | None = None,
    kind: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """One-shot event fetch."""
    params: dict[str, Any] = {"limit": limit}
    if since is not None:
        params["since"] = since
    if kind is not None:
        params["kind"] = kind
    r = await client.get("/v1/events", params=params)
    r.raise_for_status()
    return list(r.json().get("events", []))


# --------------------------------------------------------------- fixtures


@pytest.fixture
def e2e_app(tmp_path: Path) -> Any:
    """Build an in-process gateway with memory enabled and a
    scratch ``hub`` project pre-registered.

    Lifespan is not entered (see module docstring).
    """
    cfg = build_test_config(tmp_path, memory_enabled=True)
    # Wire the detach threshold below the approval timeout so
    # detach-lifecycle tests can flip the behaviour per-test via
    # the test's build step. Tests that don't use detach won't
    # trip it.
    cfg.tools = {
        "approval_timeout_secs": 5.0,
        "approval_detach_threshold_secs": 0.05,
    }
    app = create_app(cfg)

    # Pre-register a project so tools that take ``project=...`` can
    # resolve. The path is scratch; tools never actually execute
    # through the approval path in these tests (approvals resolve
    # or reject before reaching the backend).
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    app.state.project_registry.add(
        Project(
            name="hub",
            ssh_host="",
            path=str(repo),
            test_command="pytest -q",
        )
    )
    return app


@pytest.fixture
async def e2e_client(e2e_app: Any) -> AsyncIterator[httpx.AsyncClient]:
    """Async HTTP client bound to the gateway's ASGI transport.

    Bearer token pre-attached; the auth middleware resolves it
    to ``client="webui"`` (untagged token default). Tests that
    need a different client identity can override the
    ``Authorization`` / ``X-FITT-Client`` headers on individual
    requests.
    """
    transport = httpx.ASGITransport(app=e2e_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {PERSONAL_TOKEN}"},
    ) as client:
        yield client


@pytest.fixture
def e2e_approver(e2e_client: httpx.AsyncClient) -> E2EApprover:
    """An approver bound to the same HTTP client.

    Default ``client_tag`` is ``"webui"`` to match the untagged
    test token. Tests that want to simulate a Telegram approval
    can construct their own ``E2EApprover(e2e_client,
    client_tag="telegram")`` — don't forget to also tag the
    originating chat request with ``X-FITT-Client: telegram``
    (and a matching token) or the approval will be created for
    ``webui`` and the telegram-tagged approver won't see it.
    """
    return E2EApprover(e2e_client, client_tag="webui")


@pytest.fixture
def e2e_clock() -> E2EClock:
    return E2EClock()


@pytest.fixture
def stubbed_llm(monkeypatch: pytest.MonkeyPatch) -> StubbedLLM:
    stub = StubbedLLM()
    monkeypatch.setattr("gateway.router.litellm.acompletion", stub._dispatch)
    return stub
