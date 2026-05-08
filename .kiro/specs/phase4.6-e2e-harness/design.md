# Phase 4.6 — E2E test harness: Design

## Overview

One pytest harness that drives the full gateway pipeline
in-process. Real HTTP (via ASGI transport), real routing, real
approval middleware, real scheduler, real event log. Stubbed
LLM. No real Telegram, no Docker, no network.

The target usage shape:

```python
async def test_cron_fires_once_and_pushes_one_event(
    e2e_app, e2e_client, e2e_approver, e2e_clock, stubbed_llm
):
    stubbed_llm.load([
        stub_tool_call("cron_add", {...}),       # user asks for cron
        stub_reply("Cron created."),
        stub_reply("Briefing: nothing urgent."), # later, when the cron fires
    ])
    async with e2e_approver.approving("cron_add"):
        r = await e2e_client.post("/v1/chat/completions", json=...)
        assert r.status_code == 200

    e2e_clock.advance(3600)
    await e2e_clock.run_due()

    events = await fetch_events(e2e_client)
    kinds = [e["kind"] for e in events]
    assert kinds.count("cron_fired") == 1
    assert kinds.count("cron_completed") == 1
```

## Architecture

```
                +--------------------- pytest event loop -------------------+
                |                                                           |
   stubbed_llm  +--- monkeypatch gateway.router.litellm.acompletion         |
   (sequence)   |                                                           |
                |         +---------- in-process gateway ------------+      |
   e2e_clock    |         |                                          |      |
   (time knob)  +-------->+ CronScheduler.tick(now=...)              |      |
                |         | ApprovalMiddleware                       |      |
                |         | CronRunner, EventLog, MemoryStore        |      |
                |         | /v1/chat/completions, /v1/events,        |      |
                |         | /v1/approvals/*                          |      |
                |         +------------------------------------------+      |
                |                    ^                    ^                 |
   e2e_client   +--------------------+                    |                 |
   (httpx.AsyncClient + ASGITransport)                    |                 |
                |                                         |                 |
   e2e_approver +-----------------------------------------+                 |
   (polls /v1/approvals/pending, POSTs /v1/approvals/<id>/decide)           |
                |                                                           |
                +-----------------------------------------------------------+
```

Four principles.

1. **One event loop.** The HTTP client, the approval futures, the
   detached workers, the scheduler, the approver poller — all
   on the same pytest asyncio loop. This is why we reach for
   `httpx.AsyncClient(ASGITransport(app))` instead of
   `TestClient` (which spawns a thread and breaks the Future-
   bound-to-loop invariant the approval middleware relies on;
   `test_detach.py` already ate that lesson).

2. **Time is explicit.** No `asyncio.sleep(0.5)` "hoping the
   scheduler ticked." Tests call `e2e_clock.advance(3600)` and
   then `await e2e_clock.run_due()` which invokes
   `CronScheduler.tick(now=self._now)` once. The scheduler's
   background task is never started in tests.

3. **LLM stays stubbed.** `_llm_stubs.stub_sequence([...])`
   gives the suite deterministic, microsecond-fast model
   responses. Real-model quality remains `llm-checker
   toolcheck`'s territory; this suite pins code behaviour.

4. **The boundary is HTTP.** Tests make real `/v1/*` requests
   the way the bot does. Inside the gateway everything runs
   through normal auth, normal routing, normal dispatch,
   normal event emission, normal memory persistence. Whatever
   a live gateway would do, these tests do — minus the
   network tails (Telegram API, Ollama, SSH).

## Fixtures

All five live in `gateway/tests/e2e/conftest.py`. They compose —
a test can request any subset; they don't assume ordering.

### `e2e_app`

Async fixture. Returns a `FastAPI` app built via
`create_app(build_test_config(tmp_path, memory_enabled=True))`.
Memory is on so history-assertion tests work. A `hub` project is
pre-registered pointing at a scratch directory so tools that
take `project=...` resolve correctly (copy of the pattern from
`test_detach._build_app`).

Lifespan is not entered. This matters: `create_app` registers
`on_event("startup")` hooks for MCP and the cron scheduler. In
tests we drive the scheduler manually via `e2e_clock`, and MCP
never runs (no configured servers). `TestClient`'s lifespan
handling would start both; `AsyncClient(ASGITransport(app))`
does not. Good for us.

### `e2e_client`

`httpx.AsyncClient(transport=httpx.ASGITransport(app=e2e_app),
base_url="http://testserver")` with `Authorization: Bearer
<PERSONAL_TOKEN>` pre-set. Yields the client, closes on
teardown.

### `e2e_approver`

A small async helper (not a coroutine the test awaits directly;
an object with methods). Two calling shapes:

**Scripted.** The common case. Build an approver once, hand it
a policy — "approve anything matching `cron_add`", "reject
anything matching `project_shell` with unsafe args" — and start
its background poller. The poller runs until the test's
asyncio-block ends (via `async with e2e_approver.start(policy):`).
While running, it polls `/v1/approvals/pending` every 10ms,
and for every pending approval not yet decided, applies the
policy.

**One-shot.** For tests that want to inspect the pending
approval before deciding: `await e2e_approver.wait_for(tool="cron_add")`
returns the PendingApproval dict, then `await
e2e_approver.decide(id, "approve")` posts the decision. The
test controls the latency explicitly.

The scripted shape is what 90% of tests will use. The
one-shot shape is what the detach test uses (it deliberately
waits past the detach threshold before deciding).

### `e2e_clock`

Explicit time control for the scheduler.

```python
class E2EClock:
    def now(self) -> float: ...
    def advance(self, seconds: float) -> None: ...
    async def run_due(self, scheduler: CronScheduler | None = None) -> list[str]: ...
```

`advance` mutates an internal `_now` float. `run_due` calls
`scheduler.tick(now=self._now)` and awaits every in-flight
firing task it spawned so the test can assert on events
immediately afterwards.

No clock control for `time.time()` inside running code. Where
the gateway calls `time.time()` directly (event timestamps,
approval `created_at`), tests tolerate real wall-clock. Only
the scheduler's "is this job due" decision is under test
control, and that's enough for the 5 lifecycle tests in U1.

`CronScheduler.tick(now=...)` already accepts the hook. No
gateway change needed.

### `stubbed_llm`

Wraps `_llm_stubs.stub_sequence` into a reusable per-test
fixture:

```python
@pytest.fixture
def stubbed_llm(monkeypatch):
    class Stub:
        def __init__(self) -> None:
            self.calls: list[dict] = []
            self._queue: list[Any] = []
        def load(self, responses: list[Any]) -> None: ...
        def remaining(self) -> int: ...
    stub = Stub()
    async def _fake(**kwargs: Any) -> Any:
        stub.calls.append(kwargs)
        if not stub._queue:
            raise AssertionError("stubbed_llm: no more responses queued")
        return stub._queue.pop(0)
    monkeypatch.setattr("gateway.router.litellm.acompletion", _fake)
    return stub
```

`stub.calls` captures every dispatch so tests can assert on
which alias resolved, what system prompt was sent, whether the
tool schemas made it through, etc.

## Test files

Five lifecycle tests, one file per U1 acceptance criterion.

### `test_cron_lifecycle.py`

Happy path end-to-end (U1.1). Covers the original Phase 4.5
Task 7 ask: drive the whole pipeline, including the push-
channel shape, without a bot.

1. Seed `stubbed_llm` with: `stub_tool_call("cron_add", ...)`,
   `stub_reply("created")`, `stub_reply("briefing done")`.
2. `e2e_approver.start(policy=approve_if_tool("cron_add"))`.
3. POST `/v1/chat/completions` with "create a 1-hour cron".
4. Assert 200 and the reply came back.
5. `e2e_clock.advance(3601)`; `await e2e_clock.run_due(scheduler)`.
6. GET `/v1/events`. Assert `cron_fired` count = 1,
   `cron_completed` count = 1. No duplicate `cron_fired`
   push (that was the 2026-05-07 bug).

### `test_narration_lifecycle.py`

U1.2. The cron firing uses `stub_narrated_tool_call(...)` — the
model writes JSON-fenced tool call in `content` with no real
`tool_calls`. Assert:
- `cron_completed` still lands (the run "completes" with the
  narration as body).
- `tool_call_narrated` also lands, with `meta.tool_name` equal
  to the narrated tool.

This pins the observability contract from Task 10 (narration
detector) at the HTTP-through-events layer. There's already a
unit test for it in `test_cron_runner.py`; the e2e version is
the insurance that a future refactor can't break the wiring.

### `test_default_alias_lifecycle.py`

U1.3. The 'models are configuration' principle has a
lifecycle-shaped failure mode: an accidental `fitt-smart`
silent default sneaks back in as a convenience. The e2e test
pins the contract.

1. `build_test_config` exposes `aliases={"fitt-default":
   "qwen-big", "fitt-smart": "openrouter-sonnet", ...}`.
2. Create a cron with no `agent_alias` set.
3. Advance the clock and tick.
4. Assert `stubbed_llm.calls[-1]["model"]` resolved from
   `fitt-default` (not `fitt-smart`). The router records the
   concrete model id; we check it via the dispatch.
5. Counter-case: set `agent_alias="fitt-smart"` explicitly and
   assert the operator's choice is respected.

### `test_detach_lifecycle.py`

U1.4. Mirrors `test_detach.py` but uses the standard harness
fixtures so future tests can copy the shape.

1. Threshold ≪ approval_timeout.
2. POST `/v1/chat/completions`; assert the `⏳ Approval pending`
   placeholder body and `X-FITT-Detached: 1` header arrive
   synchronously.
3. `e2e_approver.wait_for(tool="write_file")`; decide
   `"approve"`.
4. GET `/v1/events` until `late_tool_result` lands (bounded
   async wait — 3s deadline, same pattern as `test_detach`'s
   `_wait_for_event` helper).
5. Assert both halves of the turn are in memory.

### `test_session_poisoning_lifecycle.py`

U1.5. Tool-turn poisoning: a session's history contains a
prior failed `cron_add` (the 2026-05-07 shape) and the next
turn triggers duplicate tool calls.

1. Seed the session's history file directly — write a
   markdown turn where the assistant's stored reply is "SSH
   is unreachable" (the observed poisoning pattern). This is
   the Phase-5-to-be-fixed invariant described in the
   roadmap.
2. POST a new request ("create the cron").
3. Assert the test's captured `stubbed_llm.calls` show the
   system prompt injected the stale reply into context.
4. Mark the test `xfail(reason="Phase 5 will persist
   tool-call turns structurally", strict=True)`. `strict=True`
   means an unexpected pass fails the suite — flips green on
   Phase 5 completion, which is the signal.

## Non-goals

Per U5.1, enumerated with pointers so the exclusion is not a
blind spot.

- **Real Telegram bot API.** The bot's `concurrent_updates` +
  approval-poller + event-pusher behaviour is covered by
  `telegram-bot/tests/`. Here we test up to the HTTP surface
  the bot would poll. What the bot does with a delivered event
  is the bot's own suite.
- **docker-compose integration.** Compose is a deployment
  concern (mounts, networks, health checks). Covered by live
  boot validation on the NAS. The harness runs in-process so
  the suite stays deterministic and fast per U4.
- **Real model calls.** `_llm_stubs` is the intentional
  abstraction. Real-model trajectory validation is
  `llm-checker toolcheck`'s job; keeping it out of pytest
  keeps tests fast and deterministic per U4.
- **PTB internal dispatch.** Already regressed by
  `test_build_application_enables_concurrent_updates` at the
  bot-side unit level. The deadlock surface is bot-internal;
  testing it through an HTTP harness wouldn't add coverage.
- **Real SSH reachability.** Covered by
  `tests/test_execution_backend_ssh.py` at the backend unit
  level and by operators running `fitt project test <name>`
  during setup. The harness never dispatches a real tool
  whose execution touches SSH; approvals are resolved or
  rejected before the backend would run.
- **Real Ollama / OpenRouter.** Same reasoning as real model
  calls. Router and LiteLLM integration are covered by unit
  tests (`test_router.py`) + live-boot sanity checks.

## Testing strategy per U1

Each lifecycle test has the shape:
1. Build fixtures (one line each).
2. `stubbed_llm.load([...])` — the model trajectory.
3. Start the `e2e_approver` in scripted mode (or hold a
   one-shot handle).
4. Drive: HTTP request(s) + clock advance(s) + tick(s).
5. Assert on HTTP responses + `/v1/events` + memory files.

Event-polling assertions use a small `_wait_for_event` helper
(copied from `test_detach.py`) that polls `/v1/events?since=...`
every 10ms up to a 3s deadline. No fixed sleeps.

## Time control

`CronScheduler.tick(now=...)` already accepts a `now` parameter.
No code change needed. The `E2EClock` wraps this and also
awaits every in-flight firing task the tick spawned, so the
next line of the test body can assert on events.

Tests do not control `time.time()` inside the running app. If
a test ever needs to (e.g. pin an event's timestamp for diff
assertions), we'd reach for `freezegun` at that point — not
up front.

## Approver helper

Sketch:

```python
class E2EApprover:
    def __init__(self, client: httpx.AsyncClient, *, client_tag: str) -> None: ...

    @contextlib.asynccontextmanager
    async def start(self, policy: Callable[[dict], str | None]) -> AsyncIterator[None]:
        """Run a poller until the context exits. ``policy``
        returns the decision ("approve" / "reject" / "trust_session")
        or ``None`` to skip this approval this pass."""

    async def wait_for(
        self, *, tool: str | None = None, timeout_s: float = 3.0
    ) -> dict: ...

    async def decide(self, approval_id: str, decision: str) -> None: ...
```

The scripted mode runs `policy(pending_dict)` on every pending
approval every 10ms. Returning a decision string posts it.
Returning `None` leaves the approval alone (useful for "reject
the second one, approve the first").

Client tag defaults to `"webui"` to match what the
`PERSONAL_TOKEN` test fixture normalises to. Tests that want
to simulate a Telegram approval can pass `client_tag="telegram"`.

## Stubbed LLM

Already covered by `_llm_stubs.py` (Task 11). `stub_sequence`
returns an async callable; the `stubbed_llm` fixture wraps it
with a `calls` list and a reloadable queue so tests can
inspect dispatch kwargs and load new trajectories mid-test.

Exhausting the queue raises `AssertionError("stubbed_llm: no
more responses queued")` — clearer than `StopIteration` at
a failure site. The test body learns exactly how many more
responses it needs to queue.

## Rollout

Implementation order:

1. `tests/e2e/__init__.py` + `tests/e2e/conftest.py` with all
   five fixtures. No tests yet; fixtures alone should be
   green under `pytest gateway/tests/e2e/ -q`.
2. `test_cron_lifecycle.py` — happy path, proves the harness.
3. `test_narration_lifecycle.py` — narration detector wiring.
4. `test_default_alias_lifecycle.py` — alias contract.
5. `test_detach_lifecycle.py` — detach + late event.
6. `test_session_poisoning_lifecycle.py` — xfail(strict=True)
   for the Phase 5 fix.

Each step commits independently so a reviewer can see the
fixtures land, then one test per commit.

## Open design decisions

1. **Where does `e2e_approver` live — conftest or a module?**
   v0 puts it in `conftest.py` as a fixture returning an
   `E2EApprover` instance. If it grows past ~60 lines we
   move the class to `tests/e2e/_approver.py` and keep the
   fixture a one-liner.

2. **Do we want a shared `test_harness` helper for
   `_wait_for_event` / `_wait_for_pending`?** v0 copies
   `test_detach.py`'s helpers into the e2e conftest. If
   `telegram-bot/tests/` ever needs the same helpers we
   extract to a `gateway/tests/_async_wait.py` — Phase 4.7
   work, not this phase.

3. **Memory-on vs memory-off per test.** v0 turns memory on
   globally (matches what production does). A test wanting a
   clean-slate session can use a fresh `session_key`. If
   test isolation ever bites, we'd flip the default or add a
   parametrised marker — not anticipating it today.

4. **Clock control for `time.time()`.** v0 controls only
   `CronScheduler.tick(now=...)`. If a future test needs a
   frozen wall clock (e.g. pinning event `ts` for snapshot
   tests) we adopt `freezegun` at that point. Deferred so the
   harness ships smaller.

5. **Parallel test execution.** Each test creates its own
   `tmp_path`-backed gateway via `e2e_app`. No shared state
   between tests. `pytest -n auto` should be safe; verified
   during rollout.

6. **Slow-test tagging.** The whole e2e suite targets <10s
   total per U4.1. If any test approaches 1s on its own, it
   gets a `@pytest.mark.slow` marker so `-m "not slow"`
   keeps the inner dev loop snappy. No marker needed yet;
   the first harness test that goes over budget gets one.

## Correctness properties

Hoisted from the test strategy into one list so a reviewer
can check them off:

- **P1. One cron firing → one `cron_fired` + one
  `cron_completed`.** (U1.1)
- **P2. Narration → `tool_call_narrated` event, regardless of
  `cron_completed`.** (U1.2)
- **P3. No `agent_alias` → `fitt-default` resolution.** (U1.3)
- **P4. Detach → synchronous placeholder + asynchronous
  `late_tool_result`.** (U1.4)
- **P5. Seeded stale history should not produce duplicate tool
  calls on the next turn.** (U1.5, currently xfail)
- **P6. `e2e_app` + `e2e_client` + `e2e_approver` + `e2e_clock`
  compose without ordering constraints.** (U3.1)

P1-P5 are test-level. P6 is fixture-level and validated by the
fact that all five lifecycle tests use overlapping subsets of
the four fixtures and all pass.
