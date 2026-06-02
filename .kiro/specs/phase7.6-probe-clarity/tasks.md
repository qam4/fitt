# Implementation Plan: FITT Phase 7.6 — Probe Clarity

## Overview

Make the alias probe and eval classify failures honestly, tell
"slow/loading" apart from "unreachable", probe shared-endpoint
aliases sequentially, and surface it all through a unified
per-alias dashboard page without bloating the aliases table.

Implementation order keeps the tree green at every commit.
Each top-level group is a reviewable commit. The extractions
(taxonomy, reachability) land first as pure no-behavior-change
refactors, pinned by the existing chat/health suites, before
anything consumes them.

Status legend: `[x]` done, `[ ]` not yet.

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["Commit 1", "Commit 2"],
      "rationale": "Independent pure-refactor extractions (taxonomy, reachability), pinned green by existing chat/health suites. No consumers yet."
    },
    {
      "wave": 2,
      "tasks": ["Commit 3"],
      "rationale": "Probe adopts the taxonomy (Commit 1) and the reachability ping (Commit 2) for failure classification + latency."
    },
    {
      "wave": 3,
      "tasks": ["Commit 4", "Commit 5"],
      "rationale": "Commit 4 (sequential per-endpoint + per-alias endpoint) builds on the new probe (Commit 3). Commit 5 (eval adopts taxonomy) builds on Commits 1+2. Independent of each other."
    },
    {
      "wave": 4,
      "tasks": ["Commit 6"],
      "rationale": "Unified per-alias page renders the output of the new probe (3/4) and eval (5)."
    },
    {
      "wave": 5,
      "tasks": ["Commit 7"],
      "rationale": "Lean aliases table links to the per-alias page from Commit 6."
    },
    {
      "wave": 6,
      "tasks": ["Commit 8"],
      "rationale": "Docs describe the shipped behavior; last."
    }
  ]
}
```

Commits 1 and 2 are independent of each other and land first.
Commit 3 depends on both (probe uses the taxonomy and the
reachability ping). Commit 4 depends on 3. Commit 5 depends on
1+2. Commit 6 depends on 3/4/5 (it renders their output).
Commit 7 depends on 6 (table links to the page). Commit 8
(docs) last.

## Tasks

## Commit 1 — Shared dispatch-outcome taxonomy (no behavior change)

Goal: extract the chat path's classifier to a shared module;
chat.py consumes it; existing chat tests stay green.

- [ ] 1a. Create `gateway/src/gateway/dispatch_outcome.py` with
       `DispatchStatus` Literal (`upstream_silent`,
       `upstream_rate_limited`, `upstream_client_error`,
       `upstream_server_error`, `unreachable`), the
       `DispatchOutcome` frozen dataclass (status, error_class,
       error_detail, upstream_status, retry_after), and
       `classify_dispatch_exception(exc) -> DispatchOutcome`.
       Logic lifted verbatim from `chat.py::_classify_upstream_error`.
       (Req 1.1, 1.2, 1.3)
- [ ] 1b. Rewire `chat.py` to import and use
       `classify_dispatch_exception`; reduce
       `_classify_upstream_error` to a thin adapter or remove
       it and update call sites. Keep the emitted
       `error_type`/`error_class`/`upstream_status` identical.
       (Req 1.4, 9.1)
- [ ] 1c. Write `gateway/tests/test_dispatch_outcome.py`: each
       exception shape maps to the right status (timeout →
       `upstream_silent`, 429/529 → `rate_limited`, 4xx →
       `client_error`, 5xx/ConnectError → `server_error`),
       error_class + detail recorded. (Req 1, Property 1)
- [ ] 1d. Property test (hypothesis): random exception types
       all classify to exactly one status, none escape.
       (Property 1)
- [ ] 1e. Confirm `test_chat_error_logging.py` passes unchanged.
       (Req 9.1, Property 4)
- [ ] 1f. ruff format/check, mypy, pytest green in `gateway/`.
- [ ] 1g. Commit: `dispatch: extract shared outcome taxonomy`.

## Commit 2 — Reachability extraction (no behavior change)

Goal: extract `/ready`'s per-model ping to a shared function;
`health.py` consumes it; `/ready` behavior identical.

- [ ] 2a. Create `gateway/src/gateway/reachability.py` with
       `ReachabilityResult` (reachable, latency_ms, detail) and
       `async check_reachable(model, *, timeout_s=2.5) ->
       ReachabilityResult`. Logic lifted from
       `health.py::_probe_model`. (Req 2.4)
- [ ] 2b. Rewire `health.py` to import `check_reachable`; keep
       `/ready` response shape + status code identical.
       (Req 2.4, 9.2)
- [ ] 2c. Write `gateway/tests/test_reachability.py`: reachable
       / unreachable / timeout per backend (ollama, openrouter,
       anthropic), latency recorded. (Req 2.5)
- [ ] 2d. Confirm `test_health.py` passes unchanged.
       (Req 9.2, Property 5)
- [ ] 2e. ruff/mypy/pytest green.
- [ ] 2f. Commit: `reachability: extract /ready ping helper`.

## Commit 3 — Probe adopts taxonomy + reachability-on-timeout + latency

Goal: the probe stops emitting bare `transport_error`; on
timeout it disambiguates slow vs unreachable; records latency.

- [ ] 3a. `ProbeResult` (`alias_probe.py`): add `latency_ms: int
       = 0` and `reachable: bool | None = None`; expand
       `ProbeStatus` with `upstream_silent`, `unreachable`,
       `upstream_rate_limited`, `upstream_client_error`,
       `upstream_server_error` (keep `ok`/`narrated`/`truncated`/
       `skipped_no_api_key`/`disabled`). (Req 1.5, 5.1, 2.6)
- [ ] 3b. `probe_alias`: time the dispatch (record `latency_ms`).
       On `TimeoutError`, call `check_reachable`; set
       `upstream_silent` (reachable) or `unreachable` (not),
       with `reachable` recorded. On other dispatch exception,
       call `classify_dispatch_exception` and map to the status.
       (Req 2.1, 2.2, 2.3, 5.1)
- [ ] 3c. The "empty reply, no tool_calls" branch: reclassify
       from today's `transport_error` to a clearer status
       (`upstream_server_error` or a dedicated `empty_reply` —
       decide in implementation; document the choice). Keep it
       distinct from the timeout path.
- [ ] 3d. Extend `test_alias_probe.py`: timeout+reachable →
       `upstream_silent`; timeout+unreachable → `unreachable`;
       latency recorded; other-exception paths classify via the
       shared taxonomy. Mock `check_reachable` + the router.
       (Req 2, 5, Property 2)
- [ ] 3e. ruff/mypy/pytest green.
- [ ] 3f. Commit: `probe: honest failure taxonomy + reachability`.

## Commit 4 — Sequential same-endpoint probing + per-alias trigger

Goal: stop the self-inflicted concurrent-VRAM timeouts; expose
per-alias probe.

- [ ] 4a. `probe_all_aliases`: group aliases by resolved
       endpoint; probe endpoints concurrently but aliases within
       an endpoint sequentially. Update the now-false "no
       contention" docstring. (Req 3.1, 3.2, 3.3)
- [ ] 4b. Property test (hypothesis): for random endpoint→alias
       groupings, no two same-endpoint canaries are in-flight at
       once; distinct-endpoint ones may overlap. (Property 3)
- [ ] 4c. `gateway/src/gateway/probe_endpoint.py` (or extend an
       existing endpoint module): `POST /v1/probe/<alias>` —
       bearer-gated, runs `probe_alias`, returns the
       `ProbeResult` JSON, updates `app.state.alias_probe_results`,
       404 typed envelope for unknown alias. (Req 4.1-4.4)
- [ ] 4d. Write `gateway/tests/test_probe_endpoint.py`: shape,
       auth, 404, updates app.state. (Req 4)
- [ ] 4e. ruff/mypy/pytest green.
- [ ] 4f. Commit: `probe: sequential per-endpoint + per-alias endpoint`.

## Commit 5 — Eval adopts the shared taxonomy

Goal: eval's failure side stops saying `transport_error`; the
verdict maps the new statuses sensibly.

- [ ] 5a. `alias_eval.py`: `run_eval_case` failure side —
       timeout → reachability-disambiguated `upstream_silent` /
       `unreachable`; other dispatch exceptions →
       `classify_dispatch_exception`. Keep success-shape
       statuses. (Req 1.5)
- [ ] 5b. `_eval_verdict` (dashboard views): map `unreachable`
       to an "incomplete — couldn't reach" verdict (not
       "risky"); `upstream_silent` to incomplete/transient.
- [ ] 5c. Extend `test_alias_eval.py` + the verdict tests.
- [ ] 5d. ruff/mypy/pytest green.
- [ ] 5e. Commit: `eval: adopt shared failure taxonomy`.

## Commit 6 — Unified per-alias dashboard page

Goal: one page per binding; absorb the eval view; add probe
detail + endpoint + "shares with".

- [ ] 6a. `_build_alias_page_context` in dashboard `views.py`:
       assemble config (model/backend/fallback/endpoint), probe
       detail (status, latency, reachability, dimensions), eval
       (reuse `_build_eval_context`'s three-suite assembly),
       context window, 24h dispatches, and the "shares with"
       computation (other aliases on the same endpoint).
       (Req 6.1, 6.3)
- [ ] 6b. New route `/dashboard/alias/<id>` + `alias_page.html`
       template with the sections and the two action buttons
       (run eval ▾, re-probe this alias). (Req 6.1, 6.4)
- [ ] 6c. Redirect `/dashboard/eval/<alias>` →
       `/dashboard/alias/<id>#eval` (or remove + re-point the
       table badge). No eval info lost. (Req 6.2)
- [ ] 6d. Per-alias re-probe action route
       `/dashboard/actions/reprobe-alias` (one alias), CSRF +
       typed-action substrate, calls the per-alias probe.
       (Req 4.5)
- [ ] 6e. Tests: alias page renders all sections; eval content
       present; "shares with" line appears for shared endpoints,
       absent for solo; per-alias re-probe action; eval redirect.
       (Req 6)
- [ ] 6f. ruff/mypy/pytest green.
- [ ] 6g. Commit: `dashboard: unified per-alias page`.

## Commit 7 — Lean table + endpoint column + amber/red pip + latency

Goal: the aliases table becomes a clean index pointing at the
alias page.

- [ ] 7a. `_build_aliases_context`: add `endpoint`; build the
       compact probe summary (`✓ 1.2s` / `⏳ slow 10s+` /
       `✗ unreachable`); compute the amber/red/green/grey pip
       from the expanded status set. (Req 7.1, 8.1-8.4, 5.2)
- [ ] 7b. `_aliases_panel.html`: add endpoint column; replace
       the F19 inline probe-detail tooltip with the compact
       summary linking to `/dashboard/alias/<id>`; eval badge
       links to the alias page. Remove the now-redundant inline
       detail. (Req 7.1, 7.2, 7.3)
- [ ] 7c. Tests: endpoint column present; compact probe summary
       + link; pip color per status; no full probe detail inline.
       (Req 7, 8)
- [ ] 7d. ruff/mypy/pytest green.
- [ ] 7e. Commit: `dashboard: lean aliases table + endpoint column`.

## Commit 8 — Docs

- [ ] 8a. `gateway/README.md`: document the dispatch-outcome
       statuses, reachability-on-timeout, sequential probing,
       `/v1/probe/<alias>`, and the unified alias page. Note the
       two distinct timeout knobs (reachability 2.5s vs probe
       inference budget).
- [ ] 8b. `docs/observed-issues.md`: the 2026-05-28 "1 of 3
       transport_error on shared-GPU laptop" incident, root
       cause (concurrent probe + VRAM contention + flat
       taxonomy), and how this phase resolves it.
- [ ] 8c. Commit: `docs: probe clarity (Phase 7.6)`.

## Verification (manual, on the hub)

- [ ] V1. Re-probe all from the dashboard against the 3-laptop-
       model setup. Confirm: probes run sequentially per
       endpoint (no longer 1-of-3); cold-loading models show
       `upstream_silent`/amber with latency, not
       `transport_error`/red.
- [ ] V2. Pull the laptop's network / sleep it; re-probe.
       Confirm aliases show `unreachable`/red, distinct from the
       slow case.
- [ ] V3. Open `/dashboard/alias/<id>` for a laptop alias.
       Confirm probe detail, latency, reachability verdict, the
       three eval suites, endpoint, and "shares with" line all
       render.
- [ ] V4. Per-alias re-probe one binding; confirm clean single-
       model result without disturbing siblings.
- [ ] V5. `/ready` and a live Telegram tool turn still behave
       exactly as before (no regression).

## Definition of done

- All required tasks complete (or explicitly deferred with a
  cross-reference).
- `test_chat_error_logging.py` and `test_health.py` green
  unchanged (Properties 4, 5).
- Standard test/lint/typecheck cycle green in both packages.
- Live validation V1-V5 done on the hub.
- Roadmap / steering pointer updated if the phase shifts the
  plan.

## Notes

- **Extractions land first.** Commits 1 and 2 are pure
  refactors with no behavior change, pinned green by the
  existing `test_chat_error_logging.py` and `test_health.py`
  suites. Nothing consumes the shared modules until they're
  proven equivalent.
- **Two timeout knobs stay separate.** The reachability ping
  uses ~2.5s (`/ready`'s value); the inference canary keeps its
  longer `boot_probe_timeout_s`. They measure different things;
  do not unify.
- **`unreachable` is probe-only.** The chat path never runs the
  reachability ping, so it never emits `unreachable` — it keeps
  lumping connection failures into `upstream_server_error`. The
  taxonomy defines the status; not every consumer emits every
  status. This is intentional, not drift.
- **The eval view is absorbed, not rewritten.** Commit 6 moves
  the F18 eval content into the unified page; the rendering
  logic is reused, not reimplemented.

## Deferred — see design.md "Future Extensions"

- TTFT measurement (stream the canary).
- Endpoints page (only if endpoints gain actions).
- Probe progress UX for the slower sequential re-probe-all.
- VRAM-aware capacity hints.
- Per-endpoint timeout config.
