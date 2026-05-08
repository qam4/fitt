# Phase 4.6 — E2E test harness: Tasks

Status legend: `[x]` done, `[ ]` not yet.

## 1. Spec promotion

- [x] 1a. Promote the Phase 4.6 inline draft from
       `FITT_ROADMAP.md` to the three-file spec here:
       `requirements.md`, `design.md`, `tasks.md`.
- [x] 1b. Commit the spec separately from the implementation
       so the rationale has its own change in history.

## 2. Harness fixtures (`tests/e2e/conftest.py`)

- [x] 2a. `tests/e2e/__init__.py` (empty; package marker for
       pytest collection).
- [x] 2b. `e2e_app` fixture: `create_app(build_test_config(tmp_path,
       memory_enabled=True))` with a `hub` project pre-registered
       pointing at a scratch directory.
- [x] 2c. `e2e_client` fixture: `httpx.AsyncClient(
       ASGITransport(app=e2e_app), base_url="http://testserver"
       )` with `Authorization: Bearer <PERSONAL_TOKEN>`
       pre-attached.
- [x] 2d. `e2e_approver` fixture returning an `E2EApprover`
       instance. Supports both `start(policy)` scripted mode
       and `wait_for` / `decide` one-shot mode. Polls
       `/v1/approvals/pending?client=<tag>` every 10ms and POSTs
       `/v1/approvals/<id>/decide`.
- [x] 2e. `e2e_clock` fixture: `E2EClock` with `now()`,
       `advance(seconds)`, `run_due(scheduler)` (tick the
       scheduler at the current virtual `now` and await all
       spawned firing tasks).
- [x] 2f. `stubbed_llm` fixture: wraps `stub_sequence` from
       `_llm_stubs` into a reusable object with `.load(responses)`,
       `.calls`, `.remaining()`. Monkeypatches
       `gateway.router.litellm.acompletion` to consume from
       the queue; empty queue raises
       `AssertionError("stubbed_llm: no more responses queued")`.
- [x] 2g. `wait_for_event(client, *, kind, timeout_s=3.0)` and
       `E2EApprover.wait_for(tool=..., timeout_s=...)` helpers in
       the conftest so lifecycle tests don't re-implement them.
- [x] 2h. Composability sanity test (`test_fixtures.py`) that
       requests all five fixtures, makes one HTTP call, and
       asserts the HTTP surface is wired. Proves P6.

## 3. U1.1 — cron lifecycle (`test_cron_lifecycle.py`)

- [x] 3a. Happy path: stubbed LLM emits `cron_add` tool call,
       approver approves, initial chat returns.
- [x] 3b. Advance the clock past the cron's first-due
       timestamp; `run_due(scheduler)` fires it.
- [x] 3c. Assert exactly one `cron_fired` and one
       `cron_completed` in `/v1/events`.
- [x] 3d. Assert no duplicate `cron_fired` push (the
       2026-05-07 bug pinned).

## 4. U1.2 — narration lifecycle (`test_narration_lifecycle.py`)

- [x] 4a. Configure a cron manually (skip the tool-call path;
       test focuses on the firing).
- [x] 4b. Stubbed LLM returns `stub_narrated_tool_call(...)`
       at fire time.
- [x] 4c. Assert `cron_completed` still lands with the
       narration in body.
- [x] 4d. Assert `tool_call_narrated` lands with
       `meta.tool_name` equal to the narrated tool.

## 5. U1.3 — default alias lifecycle (`test_default_alias_lifecycle.py`)

- [x] 5a. Create a cron with no `agent_alias` set.
- [x] 5b. Advance + tick; assert the dispatch captured in
       `stubbed_llm.calls` used the concrete model bound to
       `fitt-default` (not `fitt-smart`).
- [x] 5c. Counter-case: set `agent_alias="fitt-smart"` and
       assert the explicit choice wins.

## 6. U1.4 — detach lifecycle (`test_detach_lifecycle.py`)

- [x] 6a. Configure `approval_detach_threshold_secs` well
       below `approval_timeout_secs`.
- [x] 6b. Stubbed LLM emits a `write_file` tool call; the
       approver is configured NOT to auto-decide (one-shot
       mode).
- [x] 6c. POST chat → assert synchronous `⏳` placeholder
       and `X-FITT-Detached: 1`.
- [x] 6d. Approver resolves `approve`; poll `/v1/events` for
       `late_tool_result`; assert memory has both halves.

## 7. U1.5 — session poisoning lifecycle (`test_session_poisoning_lifecycle.py`)

- [x] 7a. Seed a session's history file with a stale assistant
       reply ("SSH is unreachable").
- [x] 7b. POST a new request; assert the stale string's
       absence from dispatch — fails today, flips green on
       Phase 5.
- [x] 7c. Mark the test `@pytest.mark.xfail(strict=True,
       reason="Phase 5 persists tool-call turns structurally")`.
       When Phase 5's memory fix lands, strict-xfail flips
       the test green and forces us to un-mark.

## 8. Timing budget + flake sweep (U4)

- [ ] 8a. Run the full e2e suite 100 times locally: `for i in
       {1..100}; do uv run pytest gateway/tests/e2e/ -q ||
       break; done`. Any failure → fix before ship.
- [ ] 8b. Measure cumulative wall time: must be under 10s.
       If a single test approaches 1s, mark it `@pytest.mark.slow`.

## 9. Docs (thin)

- [x] 9a. `gateway/tests/e2e/README.md` linking to this spec
       and explaining the fixture shape (so future-us doesn't
       re-learn "why AsyncClient not TestClient").

## 10. Retrospective

- [ ] 10a. After two weeks of active work on Phase 4.7 / 5,
       check whether the harness caught any regression. Record
       the answer as a short note at the bottom of
       `design.md` under a new `## Retrospective` heading.
       If it caught zero real bugs, the value assumption was
       wrong and we re-open the phase for revision.

## Definition of done

- All fixtures in task 2 exist and compose (2h green).
- The five lifecycle tests (tasks 3-7) exist; four green,
  task 7 `xfail(strict=True)`.
- U4 timing / determinism targets met (task 8).
- `uv run pytest gateway/tests/e2e/ -q` green.
- `uv run ruff format src tests`, `uv run ruff check src tests`,
  `uv run mypy src` all clean.
- Retrospective note (task 10) scheduled for Phase 4.7 kickoff.
