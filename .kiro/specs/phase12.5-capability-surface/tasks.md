# Implementation Plan: FITT Phase 12.5 — Capability Surface

**Status:** in progress

## Overview

Consolidate the per-alias page's four overlapping measurement
cards into one Capability surface (display), keep the actions
tiered by cost (cheap "check alive" vs heavy "measure"), make the
profile runnable from the dashboard (writing where the dashboard
reads), and add the feature↔capability reconciler — the
recommendation layer (Principle 12).

Implementation order keeps the tree green at every commit and
delivers the operator's unblock first. Sub-phase 12.5a (dashboard-
run profile) is independently useful — it's the thing that lets
the author test Stage 1 without the CLI — so it lands first.

Status legend: `[x]` done, `[ ]` not yet.

## Already shipped (absorbed into this spec, marked done)

- [x] A0. Capability-profile pure layer + CLI producer (Phase 12
  task 24): `capability_profile.py`, `fitt profile alias`.
- [x] A1. Read-only Capability card on `/dashboard/alias/<id>`
  (`_build_profile_view` + `alias_page.html`), shipped 2026-06-25.
- [x] A2. Eval-report JSON sidecar + dashboard structured read
  (the markdown round-trip removed), shipped 2026-06-26.
- [x] A3. `plan-election` measured dimension (Stage 1), shipped
  2026-06-30: `measure_plan_election` in `planner.py`, wired into
  the CLI producer, auto-rendered by the card.

## Phase 12.5a — Run the profile from the dashboard (the unblock)

- [x] 1. Extract the profile producer out of `cli.py::profile_alias_cmd`
  into a reusable async `run_profile(...)` (in `capability_profile.py`
  or a new `profile_runner.py`) that takes the wired registry/
  router/approval/ctx-factory/prompt-resolver/system-prompt and
  returns a `CapabilityProfile`. CLI becomes a thin wrapper.
  (Req 1.5, 1.6, Property 5)
- [x] 2. Add `POST /v1/profile/<alias>` (mirror `/v1/eval/<alias>`
  and `/v1/probe/<alias>`): bearer-gated, builds wiring from
  `app.state`, runs `run_profile`, writes under the gateway
  `$FITT_HOME/eval/`, returns JSON, 404 typed envelope for an
  unknown alias. (Req 1.1, 1.3, 1.4, Property 2)
- [x] 3. Dashboard "measure capability" action on the alias page
  that calls the endpoint (async/typed-action pattern, like the
  eval action) and refreshes the Capability card. (Req 1.2)
- [x] 4. Tests: `test_profile_runner.py` (producer parity, faked
  results), `test_profile_endpoint.py` (shape/auth/404/writes
  under gateway `$FITT_HOME`). (Req 1, 7.3, Properties 2, 5)
- [x] 5. ruff/mypy/pytest green (both packages); commit + push.
  DONE 2026-06-30: gateway 1669 passed/8 skipped, telegram-bot 199
  passed; ruff format+check + mypy clean both packages.

## Phase 12.5b — Consolidate the surface + tier the actions

- [ ] 6. Add the cheap "check alive" action (reachability + probe
  only; reuse `/v1/probe/<alias>` + `check_reachable`) and ensure
  it dispatches NO eval/profile work. (Req 2.1, 2.3, Property 1)
- [ ] 7. Reshape `alias_page.html` + `_build_profile_view` into
  one Capability surface: liveness line, declared, measured
  (capability + cost), resources; the two cost-tiered action
  buttons with cost affordance; eval per-case detail moved behind
  a disclosure (no info lost). (Req 2.2, 2.4, 3.1, 3.2, 3.4)
- [ ] 8. Demote the three eval-suite cards to the engine behind
  "measure"; retain an advanced single-suite affordance, not as
  the headline. (Req 3.2, 3.3)
- [ ] 9. Per-dimension freshness: profile `captured_at` + probe
  timestamp shown; stale flag past a threshold; independent
  cadences. (Req 4.1, 4.2, 4.3)
- [ ] 10. (Engine single-run) Make one "measure" run each needed
  suite once and have the profile aggregate that run rather than
  re-dispatching — or, if large, land the display consolidation
  first and fold this in as the tail (design Decision 3 staging).
  (Req 6.1, 6.2, 6.3)
- [ ] 11. Tests: surface renders all blocks; cheap-action
  isolation (Property 1); freshness/stale; eval detail present
  behind disclosure. ruff/mypy/pytest green; commit + push.

## Phase 12.5c — The reconciler (the recommendation)

- [ ] 12. `capability_reconcile.py` (pure): `FeatureRequirement`,
  `FeatureReadiness`, `feature_requirements()` (model-agnostic,
  keyed on dimensions; planning → `plan-election`, with
  planner_alias borrow), `reconcile(enabled_features, profile,
  *, planner_profile) -> list[FeatureReadiness]` returning
  satisfied/unsatisfied/unknown. (Req 5.1, 5.2, 5.6)
- [ ] 13. Render feature readiness in the Capability surface;
  `unknown` points at "measure capability". (Req 5.3)
- [ ] 14. Boot warning in `app.py` (shape of `check_missing_api_keys`):
  ERROR-log each enabled-but-`unsatisfied` feature per alias;
  never refuse to start. (Req 5.4)
- [ ] 15. Guarantee surfaces-never-drives: reconciler mutates no
  config and changes no runtime behavior. (Req 5.5, Property 4)
- [ ] 16. Tests: `test_capability_reconcile.py` (totality +
  three-state Property 3, unknown-on-missing-dimension,
  planner_alias borrow, never-mutates Property 4, hypothesis
  totality); `test_app.py` boot-warning. ruff/mypy/pytest green;
  commit + push.

## Verification (manual, on the hub / home box)

- [ ] V1. From the dashboard, "measure capability" on a home-box
  alias; confirm the profile appears in the Capability surface
  with no CLI run and no `FITT_HOME` mismatch (the original
  "No capability profile on disk" symptom is gone).
- [ ] V2. "Check alive" on an alias; confirm it returns in seconds
  and triggers no eval/profile run (watch the event stream).
- [ ] V3. Profile `fitt-ec2-qwen3` and `fitt-ec2-hermes`; confirm
  feature readiness shows planning `satisfied` for qwen3 and
  `unsatisfied` for hermes (election 0%), other features
  `unknown`.
- [ ] V4. Enable `orchestration` on hermes in config, restart;
  confirm the boot warning names the unsatisfied planning feature
  and the gateway still starts.
- [ ] V5. `fitt profile alias` still works (producer parity);
  `/v1/eval` and `/v1/probe` unchanged.

## Definition of done

- 12.5a–12.5c required tasks complete (or explicitly deferred
  with a cross-reference).
- Existing probe/eval/reachability/`fitt profile` behavior
  unchanged except where layout intentionally moves content.
- Standard test/lint/typecheck cycle green in both packages.
- Live validation V1–V5 done on the hub/home box.
- BACKLOG reconciler entry + Now/Next/Later updated to point here;
  roadmap pointer if the plan shifts.

## Notes

- **12.5a is the unblock and ships first** — it's what lets the
  author test Stage 1 (plan-election) from the dashboard, the
  exact friction that triggered this spec.
- **Display vs actions** is the central decision (design Decision
  1): one surface to read, cost-tiered buttons to run. A cheap
  liveness check must never drag in evals/profile.
- **Reconciler surfaces, never drives** (Principle 12): a
  warning + a view, never an auto-toggle. v1 covers `planning`
  (the only measured dimension); other features report `unknown`
  until their dimension ships — which is how the deferred
  dimensions earn their priority.

## Deferred — see design.md "Future Extensions"

- More profile dimensions (synthesis, context-tolerance, VRAM,
  token-cost, refusal-rate, variance), pulled in by reconciler
  demand.
- `fitt doctor` CLI mirror of feature readiness.
- Auto-recommended config edits (suggestion only, never applied).
- Folding the probe into the profile as a `liveness` dimension.
