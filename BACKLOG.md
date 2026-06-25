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
- _(open - pull the top of Next)_

**Next**
- ddgs search-quality investigation (small; unblocks the synthesis retest).
- Operator-facing timeout error messages (~1 hr).

**Later**
- Consolidate the eval/profile/probe measurement sinks.
- Re-test synthesis vs relay on a capable model + working search (after
  the ddgs fix).
- Eval harness over the real registry -> then the message/text and
  edit_file ergonomics fixes.

---

## Capability, eval & observability

- **Render the profile baseline-diff in the Capability card** - the
  card now shows declared facts + measured grades + resources from
  `<alias>-profile.json` (shipped 2026-06-25); the remaining piece is
  rendering the last baseline diff / regressions alongside it.
  _(detail: [phase12 deferred](.kiro/specs/phase12-planning-execution/tasks.md))_
- **Consolidate the measurement sinks** - probe = liveness pip, eval
  suites = measurement engine, profile = aggregation + the rendered
  surface; switch the dashboard's eval cell to read structured JSON
  instead of regex-parsing the markdown header.
  _(detail: [phase12 deferred](.kiro/specs/phase12-planning-execution/tasks.md))_
- **Capability-profile dimensions beyond v1** - VRAM/cold-load,
  token-cost-per-outcome, JSON-validity, refusal rate, run-to-run
  variance, context-degradation curve. Data model already supports each
  as an append.
  _(detail: [phase12 deferred](.kiro/specs/phase12-planning-execution/tasks.md))_
- **Re-test synthesis vs relay on a capable model + working search** -
  the execute-step/capability-prompt tuning the task-26 verdict points
  at, measured on qwen3:14b (not hermes3:8b) with a search query/backend
  that returns real headlines, not homepages.
  _(source: [observed-issues 2026-06-23](docs/observed-issues.md))_
- **ddgs search quality** - `web_search` returns news-site homepages with
  boilerplate snippets instead of actual headlines; investigate query
  shaping vs backend choice.
  _(source: [observed-issues 2026-06-23](docs/observed-issues.md))_

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

- **Operator-facing timeout error messages** - name the specific config
  key and explain the provider-vs-agent timeout layering, OpenClaw-style.
  ~1 hour.
  _(source: project-overview steering)_
- **Setup recipes the agent can drive** - "help me set up X" docs written
  *for* the agent (numbered steps, exact commands, fallbacks). ~half a
  day per recipe.
  _(source: project-overview steering)_
