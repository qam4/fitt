# End-to-end test harness

This directory holds the Phase 4.6 lifecycle tests. They drive
the full gateway pipeline (HTTP → auth → routing → approval →
scheduler → event log) in-process via an ASGI transport, with
a stubbed LLM and explicit time control.

See `.kiro/specs/phase4.6-e2e-harness/` for the full spec
(requirements, design, tasks). Quick pointers:

- **conftest.py** — the five composable fixtures (`e2e_app`,
  `e2e_client`, `e2e_approver`, `e2e_clock`, `stubbed_llm`)
  plus two async helpers (`wait_for_event`, `fetch_events`).
- **test_fixtures.py** — sanity check that the fixtures compose.
- **test_cron_lifecycle.py** (U1.1) — happy-path cron firing
  end-to-end. Pins the `cron_fired` duplicate-push bug.
- **test_default_alias_lifecycle.py** (U1.3) — "models are
  configuration" contract: no silent `fitt-smart` upgrade.
- **test_detach_lifecycle.py** (U1.4) — synchronous placeholder
  + asynchronous `late_tool_result` after approval.
- **test_session_poisoning_lifecycle.py** (U1.5) —
  strict-xfail that flips green when Phase 5's structured
  tool-call persistence lands.

U1.2 (the narration lifecycle) was removed 2026-05-12
alongside the live-chat narration detector. The shape check
now only runs in `alias_probe` and `alias_eval` where the
expected tool is baked into the test case.

## Why `httpx.AsyncClient(ASGITransport)` and not `TestClient`

The approval middleware stashes an `asyncio.Future` and awaits
it; the decide handler sets it from the bot's POST. Both sides
must share one event loop or the Future never wakes.
`TestClient` spawns a thread and a separate loop per call.
`ASGITransport` keeps everything on the pytest-asyncio loop.
`test_detach.py` discovered this the hard way — preserving the
note here so the next contributor doesn't re-learn.

## Why lifespan is NOT entered

`create_app` registers `@app.on_event("startup")` for MCP and
the cron scheduler. In tests we drive the scheduler manually
via `e2e_clock.run_due(...)`, and MCP never runs (test config
has no servers). `AsyncClient(ASGITransport(app))` does not
invoke lifespan by default, which is exactly what we want.

## Adding a new lifecycle test

1. Copy one of the existing files.
2. Request the fixtures you need (any subset; they compose).
3. If you need a specific model trajectory, build it from
   `tests/_llm_stubs.py` builders (`stub_reply`,
   `stub_tool_call`, `stub_narrated_tool_call`, `stub_sequence`).
4. Drive the pipeline via `e2e_client.post(...)` +
   `e2e_clock.advance(...)` / `e2e_clock.run_due(scheduler)`
   + `e2e_approver.start(policy)` / `wait_for` / `decide`.
5. Assert on HTTP responses, `/v1/events` (via
   `fetch_events` / `wait_for_event`), and memory files.

## Budget

The whole suite runs in under 10s on a dev laptop (U4.1). If
an individual test goes over ~1s on its own, mark it
`@pytest.mark.slow` so `-m "not slow"` keeps the inner dev
loop snappy.
