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
- Eval harness over the real registry -> then the message/text and
  edit_file ergonomics fixes.

**Next**
- Render the profile baseline-diff in the dashboard Capability card.

**Later**
- Further capability-profile dimensions (VRAM, token-cost, JSON-
  validity, refusal rate, variance, context-degradation).

---

## Capability, eval & observability

- **Reconcile features <-> model capability (`fitt doctor` / a dashboard
  "Feature readiness" view)** - surfaced 2026-06-27. FITT has feature
  switches (orchestration/planning, memory, skills, web search) and a
  per-alias capability profile, but *nothing joins them*. An operator
  can set `orchestration.enabled: true` on a model that elects to plan
  0% of the time (hermes3:8b) and get a silently dead feature - the
  config example documents the knob but not which bound model can
  actually drive it. The fix is the missing middle layer:
  1. A small, model-agnostic **feature -> required-capability map**
     (planning needs plan-election on the alias or its `planner_alias`;
     a good web_search answer needs synthesis; long history needs
     context tolerance; etc.), keyed on profile *dimensions*, never
     model names.
  2. A pure **reconciler**: per alias, given its enabled features +
     its profile, report each feature as satisfied / unsatisfied (loud,
     Principle 11) / unknown (needs `fitt profile alias`). Three-state;
     "unknown" is first-class.
  3. **Surfaces**, reusing what exists: a boot warning (same shape as
     `check_missing_api_keys`, warn-don't-refuse), a dashboard "Feature
     readiness" card next to the Capability card, and optionally a
     `fitt doctor` CLI (the shareable "is my setup sane" check).
  Surfaces/warns, **never auto-disables** (the task-24 operator-in-the-
  loop commitment; measurements are sample-noisy). Declared facts first
  (free bounds from `/api/tags`), measured grades only where declared
  can't answer. Generalises the boot probe (which already checks one
  capability - tool-calling - loudly at boot) to "check the capabilities
  the enabled features require." Payoff: defining feature requirements
  is what makes the deferred profile dimensions (synthesis,
  orchestration-readiness, context-degradation) *earn their keep* - you
  measure exactly what some enabled feature needs, in priority order,
  instead of building all of them speculatively. Spec-worthy when
  started; the doc-fix half (flagging model-dependent plan election in
  the config example) shipped 2026-06-27. This is FITT's missing "detect
  optimal settings" layer - the recommendation engine that turns the
  facts-only capability profile into "enable these features for this
  model", per roadmap Principle 12 (adapt the feature set to the model
  you can run).
  _(source: this session's "how does benchmarking inform config" thread;
  related: the three capability-profile items below are sub-parts /
  data it consumes.)_

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

## Tool ergonomics & coverage

- **Eval harness should exercise the REAL registered tools** - today it
  tests synthetic re-declared schemas, so schema-ergonomics bugs in the
  shipped registry (the `cron_add` failure) are invisible by
  construction. Prerequisite for the two below.
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
