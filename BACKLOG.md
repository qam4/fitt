# FITT Backlog

Cross-cutting work that isn't owned by any phase spec - plus the one
thing that can't be derived: what to pick up next.

**Not** a tracker or a schedule. Per guiding Principle 9 (*live with it
before extending it*), items get picked when an evening goes that way,
not worked top to bottom. If it stops being a quick scan, prune it.

## What lives where

- **Phase work** -> `fitt tasks` rolls up every
  `.kiro/specs/*/tasks.md` (collapsing shipped/shelved phases via each
  spec's `**Status:**` line). The specs are the source of truth; don't
  copy their tasks here.
- **Big arcs** (Phase 8 compaction, 9 memory v1, 10 voice, 11 home
  assistant) -> [`FITT_ROADMAP.md`](FITT_ROADMAP.md).
- **Raw findings** -> [`docs/observed-issues.md`](docs/observed-issues.md).
  Items *graduate* from there into here when they turn from "noticed"
  into "worth doing."
- **This file** -> only items with no phase-spec home, plus the
  Now/Next/Later ordering.

**Lifecycle:** observed-issues (noticed) -> BACKLOG (worth doing) ->
spec (building) -> done.

## Now / Next / Later

The curated ordering - the judgment call a tool can't make for you.

**Now**
- Act on the tool-consistency lint (shipped 2026-07-01,
  `tool_consistency.py`, boot log + Settings "Boot-time warnings"
  card): **normalise the payload field name** (`cron_add` /
  `cron_update` `message` -> `text` to match `send_message` /
  `learn_add`) and **flatten `edit_file`'s required surface**. The lint
  now flags both and catches regressions, so these are safe to do.

**Next**
- Feed the eval the REAL tool forms (Lane A: `run_eval_suite` offers
  `registry.list_all()` schemas instead of hand-written lookalikes,
  cases reference tool names) — the model-side half, a bigger refactor.

**Later**
- Render the profile baseline-diff in the Capability card (folds into
  the 12.5b surface).
- Liveness bullet: fresh-shallow vs stale-deep + no auto-refresh
  (observed 2026-07-01; belongs to phase7.6-probe-clarity).
- Further capability-profile dimensions (VRAM, token-cost, JSON-
  validity, refusal rate, variance, context-degradation) - pulled in by
  12.5c reconciler demand.

---

## Capability, eval & observability

- **Capability surface + feature<->capability reconciler** - SHIPPED
  2026-07-01: [`phase12.5-capability-surface`](.kiro/specs/phase12.5-capability-surface/tasks.md).
  FITT's "detect optimal settings" layer (Principle 12): run the profile
  from the dashboard (12.5a, the no-CLI unblock), consolidate
  probe/eval/profile into one cost-tiered Capability surface (12.5b),
  and add the reconciler - per-feature `satisfied/unsatisfied/unknown`
  readiness + a boot warning, surfaces never auto-drives (12.5c). All
  three sub-phases shipped; V1-V5 hub validation closed by operator.
  The vocabulary this thread kept re-deriving now lives in
  project-overview steering ("Model capability: the measurement ladder").
  _(was: this session's "how does benchmarking inform config" thread.)_

- **Render the profile baseline-diff in the Capability card** - the
  card now shows declared facts + measured grades + resources from
  `<alias>-profile.json` (shipped 2026-06-25); the remaining piece is
  rendering the last baseline diff / regressions alongside it.
  _(detail: [phase12 deferred](.kiro/specs/phase12-planning-execution/tasks.md))_
- **Profile as single source of truth for capability** - the eval-report
  JSON sidecar + dashboard structured read shipped 2026-06-26 (the
  markdown round-trip is gone). What remains is the broader reframe:
  probe = liveness pip, profile = aggregation, fold the scenario result
  in as a dimension. Lower priority - the painful part is done.
  _(detail: [phase12 deferred](.kiro/specs/phase12-planning-execution/tasks.md))_
- **Capability-profile dimensions beyond v1** - VRAM/cold-load,
  token-cost-per-outcome, JSON-validity, refusal rate, run-to-run
  variance, context-degradation curve. Data model already supports each
  as an append.
  _(detail: [phase12 deferred](.kiro/specs/phase12-planning-execution/tasks.md))_
- **Better news search backend** - investigated 2026-06-26: ddgs
  `.text()` returns homepages for generic "today's news" queries but
  rich results for specific ones, and ddgs `.news()` is broken (Yahoo
  DNS refused). Not a small provider fix - a working news backend is a
  new provider file + config; query shaping is model-side. Low priority
  unless the news use case matters.
  _(detail: [observed-issues 2026-06-26](docs/observed-issues.md))_

- **Long-running dashboard actions are synchronous (async job + poll)** -
  validated 2026-06-30: "Measure capability" (and "run eval") hold the
  HTTP request for the whole run - minutes, because the planner pass is
  slow. Fine for now (operator-initiated, they expect a wait), but it
  ties up the connection and offers no progress. The clean fix fits what
  the dashboard already has: kick off a background run that returns
  immediately + a status fragment that polls every ~2s via HTMX
  (`hx-get` + `hx-trigger`) and swaps in the result when done - the
  job-id + status-poll shape the eval endpoint comment already flagged,
  and exactly what kiro-monitor models. The profiler's phases
  (tool-calling -> coding -> plan-election) give natural progress text;
  no progress-bar framework needed. Applies to `/v1/profile` and
  `/v1/eval` alike. Graduate when the wait bites.
  _(source: phase 12.5a live use; see phase12.5 spec deferred)_

## Tool ergonomics & coverage

- **Eval harness should exercise the REAL registered tools** - today it
  tests synthetic re-declared schemas, so schema-ergonomics bugs in the
  shipped registry (the `cron_add` failure) are invisible by
  construction. Prerequisite for the two below.
  **Framing (see project-overview "measurement ladder"):** two distinct
  subjects, don't conflate. (a) *Model* - can it tool-call? The eval
  measures this and a representative handful of cases is enough; feeding
  it the *real* tool forms (not lookalikes) is the small targeted fix, so
  the shipped `cron_add`/`edit_file` shapes finally face a model. (b)
  *Tools* - are the forms consistent/callable? That's a separate, cheap,
  *offline* check that reads whatever's registered (incl. MCP + skills
  that no hand-written per-tool case could ever cover). Don't try to
  live-eval every tool - the ladder tests the model with representatives,
  not the inventory.
  **Lane (b) SHIPPED 2026-07-01:** `tool_consistency.py` -
  `check_tool_consistency(tools)` flags payload-field-name
  inconsistency, heavy required surfaces, and empty descriptions;
  logged at boot (`tools.inconsistent_schema`) and surfaced on the
  Settings "Boot-time warnings" card (which now aggregates all boot
  checks). **Lane (a) remains:** feed `run_eval_suite` the real
  `registry.list_all()` schemas (cases reference tool names) - the
  bigger, model-side refactor (Next).
  _(source: [observed-issues](docs/observed-issues.md))_
- **Normalise "the words" tool-arg naming** - `cron_add` uses `message`,
  `send_message`/`learn_add` use `text`; the inconsistency has already
  caused a live fumble. Breaking change - wants the real-registry eval
  first.
- **Flatten `edit_file`'s fumble surface** - 4 required fields +
  match-exactly-once; at least quote the near-miss in the error.
- **Planner pass shouldn't execute tools it didn't offer** - gemma4 calls
  an executor tool from the planner pass (side effect of the
  executor-tools hint).
  _(source: [observed-issues](docs/observed-issues.md))_

## Opportunistic upgrades (OpenClaw-inspired)

- **Setup recipes the agent can drive** - "help me set up X" docs written
  *for* the agent (numbered steps, exact commands, fallbacks). ~half a
  day per recipe.
  _(source: project-overview steering)_
