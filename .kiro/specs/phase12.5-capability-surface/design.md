# Design: FITT Phase 12.5 — Capability Surface

## Overview

FITT now has four "what's true about this binding" mechanisms —
reachability, probe, eval suites, and the capability profile —
but they're surfaced as four overlapping cards on the per-alias
page, the profile is the only one that can't be run from the
dashboard, and the profiler re-runs the eval suites the eval
action already ran. The result is a page that's confusing to the
author and intimidating to a prospective end user, with no clear
"what can this model do, and what should I turn on" answer.

This phase does three things, in cost order:

1. **Make the profile dashboard-runnable** (it's the odd one out)
   and have it write where the dashboard reads, killing the
   host-vs-container `FITT_HOME` mismatch.
2. **Consolidate the display** into one Capability surface, while
   keeping the **actions tiered by cost** — a cheap "check alive"
   that never drags in evals/profile, and a separate heavy
   "measure capability." This is the crux: *unify what you read,
   separate what you run.*
3. **Add the reconciler** — the recommendation layer that turns
   the profile's facts into per-feature `satisfied / unsatisfied
   / unknown` readiness (roadmap Principle 12), surfaced in the
   same Capability surface and as a boot warning.

## Why this is a follow-up to Phase 12 (not 13)

Phase 12 (planning & execution) shipped the capability profiler
as task 24; Stage 1 (2026-06-30) added the `plan-election`
dimension. This phase is the surface + recommendation layer on
top of that profiler — a follow-on driven by living with it
(Principle 9), exactly as Phase 7.6 (probe clarity) followed
Phase 7. Numbered 12.5, not 13, so it doesn't imply it comes
after the roadmap's 8–11 arcs (compaction, memory v1, voice,
home assistant), which it does not.

## Background: the four "is this binding OK" mechanisms

This extends the Phase 7.6 table with the profile. The whole
point is that these are a **cost ladder** — each strictly more
expensive — and the operator must be able to run a cheap rung
without the expensive ones.

| Mechanism | Question | How | Cost | Dashboard-runnable today |
|---|---|---|---|---|
| `reachability.check_reachable` (7.6) | Is the host up? | `GET /api/tags` / `/v1/models`, no inference | ~2.5s | via probe/ready |
| `alias_probe` (7.1) | Can it emit `tool_calls` at all? | one canary inference | one generation | **yes** (`/v1/probe`) |
| `alias_eval` (4.11) | How reliable across a suite? | k samples × cases | minutes | **yes** (`/v1/eval`) |
| capability profile (12 task 24) | What can this model do (graded + cost + declared)? | runs the suites + plan-election + reads `/api/tags`, aggregates, diffs | minutes | **NO (CLI only)** ← gap |

The profile is the odd one out (CLI-only) and the most expensive
(it currently re-runs the eval suites). Both facts drive this
phase.

## The crux: consolidate the display, tier the actions

The page is overwhelming because it shows four *cards*. But the
fix is **not** one button that runs everything — that would make
a 2-second liveness check cost five minutes. The fix separates
two axes that the current page conflates:

- **Display** → unify. One Capability surface to *read*.
- **Actions** → keep tiered by cost. Two buttons, not one:
  - **Check alive** (cheap, seconds): reachability + probe only.
    Never touches evals or the profile.
  - **Measure capability** (heavy, minutes): runs the eval engine
    + plan-election and refreshes the profile grades.

The profile card *displays* whatever's been measured, each
dimension stamped with its own freshness, so the cheap and heavy
actions run at independent cadences and you always see what's
current vs stale.

```
Capability — fitt-default (qwen3:14b)
  ● reachable · tool-calls OK    last checked 12s ago   [Check alive]   (seconds)
  Declared: ctx 41k · tools ✓ · thinking ✓ · 9.3 GB
  Measured  last run 2d ago (stale)                     [Measure ▸]     (minutes)
    tool-calling   100%   p50 4s
    coding          80%   p50 6s
    plan-election  100%   p50 60s
  Feature readiness
    planning   ✓ satisfied (plan-election 100%)
    web-search ? unknown — run Measure
    memory     ? unknown — needs context-tolerance dimension
```

## Architecture

```
                 per-alias page  /dashboard/alias/<id>  (Phase 7.6)
                 ┌───────────────────────────────────────────────┐
                 │  Capability surface (consolidated display)     │
                 │   liveness line · declared · measured · cost   │
                 │   · resources · feature readiness              │
                 │   [Check alive]            [Measure capability]│
                 └───────┬───────────────────────────┬───────────┘
            cheap tier   │                            │  heavy tier
        ┌────────────────┴───────┐        ┌───────────┴───────────────┐
        │ reachability + probe   │        │  PROFILE PRODUCER (shared) │
        │ (exists: /v1/probe,    │        │  capability_profile_run()  │
        │  check_reachable)      │        │  - declared (/api/tags)    │
        └────────────────────────┘        │  - eval engine (suites)    │
                                           │  - plan-election           │
                                           │  - aggregate + diff + write│
                                           └───────┬─────────┬──────────┘
                                                   │         │
                              POST /v1/profile/<alias>   fitt profile alias
                              (NEW endpoint)             (CLI, same producer)
                                                   │
                                       writes <alias>-profile.{md,json}
                                       under the GATEWAY's $FITT_HOME/eval
                                       (= the dir the dashboard reads)

        reconciler (NEW pure layer)  capability_reconcile.py
          feature_requirements:  feature -> required dimension(s)
          reconcile(enabled_features, profile) -> [FeatureReadiness]
                 ▲                                   │
       boot warning (app.py,                  feature-readiness view
       like check_missing_api_keys)           (Capability surface)
```

## Key Design Decisions

### Decision 1: Unify display, tier actions by cost (the crux)

One Capability surface to read; two cost-tiered actions to run
(cheap "check alive" = reachability+probe; heavy "measure" =
eval engine + plan-election → profile). The cheap action MUST
NOT trigger any suite/profile work. The profile card displays
measured dimensions with per-dimension freshness so the two
cadences coexist.

**Why not one "refresh everything" button:** a routine liveness
check is something an operator does often; coupling it to a
multi-minute eval+profile run would make the cheap question
expensive and discourage it. The cost ladder (reachability <
probe < eval < profile) is real; the UI must respect it.

### Decision 2: Extract the profile producer; add `/v1/profile/<alias>`

The producer logic currently lives inline in the
`fitt profile alias` CLI command (`cli.py`). Extract it to a
reusable async function (e.g. `capability_profile.run_profile(...)`
or a small `profile_runner.py`) that takes the wired registry/
router/approval/ctx-factory/prompt-resolver and returns a
`CapabilityProfile`. Both the CLI and a new
`POST /v1/profile/<alias>` endpoint call it. The endpoint mirrors
`/v1/eval/<alias>` and `/v1/probe/<alias>` (bearer-gated, 404 on
unknown alias, updates any in-process state, returns JSON).

**Why this fixes the home-box mismatch:** the endpoint runs
in-process in the gateway, so it writes to the gateway's
`$FITT_HOME/eval/` — which is exactly the directory the dashboard
reads. The CLI-on-host vs gateway-in-container `FITT_HOME` split
that produced "No capability profile on disk" simply can't happen
when the trigger is the gateway itself.

**Long-running action:** the profiler takes minutes, so the
dashboard action follows the same async/typed-action pattern the
eval action already uses (kick off, surface progress/last-run,
don't block the request). Reuse, not reinvent.

### Decision 3: The eval suites are the engine, surfaced once

Today the page shows three eval-suite cards AND a profile card
that re-ran those suites. Collapse: the suites are the
measurement engine behind "measure capability"; their per-case
detail is reachable from within the Capability surface (a
disclosure), not as three peer cards. One "measure" runs each
needed suite once; the profile aggregates that run (Requirement
6) rather than the eval action and the profile action each
dispatching the suites. This is the BACKLOG "consolidate the
measurement sinks / profile as single source of truth" reframe,
realized in the UI and the run path.

**Staging note:** making "measure" run each suite exactly once
and feed both the eval view and the profile is the deeper change;
if it proves large, v1 may ship the display consolidation +
dashboard-run-profile first and fold the single-run engine in as
12.5b's tail. The redundancy is wasteful, not wrong, so it can
lag the UX fix by a slice.

### Decision 4: Per-dimension freshness, not a single "profiled at"

Because the cheap and heavy actions run at different cadences, a
single page-level timestamp would lie. Each block carries its own
"last measured" (liveness vs profile), and a measurement older
than a threshold is flagged stale rather than shown as current.
The profile JSON already stamps `captured_at`; the liveness line
uses the probe's timestamp.

### Decision 5: Reconciler as a pure layer; surfaces, never drives

A new pure module `capability_reconcile.py`:

- `feature_requirements()` → a static, model-agnostic map from
  FITT feature to the profile **dimension(s)** + threshold it
  needs (planning → `plan-election` ≥ threshold on the alias or
  its `planner_alias`; web-search-answer → a synthesis dimension;
  memory/long-history → a context-tolerance dimension; ...).
  Keyed on dimensions, never model names (the model-agnostic
  guarantee).
- `reconcile(enabled_features, profile) -> list[FeatureReadiness]`
  → per feature, exactly one of `satisfied` / `unsatisfied`
  (measured grade below requirement — loud) / `unknown`
  (dimension absent from the profile — points at "measure").
  Three-state; `unknown` is first-class.

It is consumed in two places: the feature-readiness view on the
Capability surface, and a **boot warning** in `app.py` (same
shape as `check_missing_api_keys` — warn at ERROR, never refuse
to start). It never auto-enables/disables a feature: measurements
are multi-sample-noisy, and silent behavior changes off a noisy
read are the failure mode we avoid (Principle 12 / task-24
operator-in-the-loop commitment).

**v1 coverage is honest about gaps:** only `planning` has a
measured dimension today (`plan-election`, Stage 1). Other
features map to dimensions not yet measured (synthesis,
context-tolerance), so they report `unknown` until those
dimensions ship. That's the point of the three-state design, and
it gives the deferred profile dimensions a demand-ordered
priority (Principle 12 payoff).

### Decision 6: Build on the Phase 7.6 alias page, don't add a route

Phase 7.6 already unified everything-about-a-binding onto
`/dashboard/alias/<id>` and deliberately rejected adding more
pages. This phase reshapes the *content* of that page (the
Capability surface) and adds actions; it does not add a new
route. Consistent with 7.6 Decision 7 ("a page must do something
or be a drill-down target").

## Components and Interfaces

### Component 1: profile producer extraction

```python
# capability_profile.py (or profile_runner.py)
async def run_profile(
    alias: str, *, cfg, registry, router, approval,
    make_tool_ctx, prompt_resolver, system_prompt,
    samples: int, timeout_s: float,
) -> CapabilityProfile:
    """Declared facts + tool-calling/coding/plan-election grades.
    Called by BOTH `fitt profile alias` and POST /v1/profile."""
```

`cli.py::profile_alias_cmd` becomes a thin wrapper that builds
the wiring and calls `run_profile` + writes/renders/diffs.

### Component 2: `POST /v1/profile/<alias>` (NEW endpoint)

Mirrors `/v1/eval/<alias>`. Bearer-gated; builds the same wiring
the CLI does (registry, router, auto-approve, ctx factory,
prompt resolver) from `app.state`; runs `run_profile`; writes the
profile (gateway `$FITT_HOME`); returns JSON; 404 typed envelope
for unknown alias.

### Component 3: `capability_reconcile.py` (NEW pure layer)

```python
ReadinessStatus = Literal["satisfied", "unsatisfied", "unknown"]

@dataclass(frozen=True, slots=True)
class FeatureRequirement:
    feature: str            # "planning", "web_search_answer", ...
    dimension: str          # profile MeasuredGrade name
    min_pass_rate: float    # threshold
    allow_planner_alias: bool = False  # planning: borrow a planner

@dataclass(frozen=True, slots=True)
class FeatureReadiness:
    feature: str
    status: ReadinessStatus
    detail: str             # "plan-election 100%" / "needs measure"

def feature_requirements() -> list[FeatureRequirement]: ...
def reconcile(
    enabled_features: Mapping[str, bool],
    profile: CapabilityProfile | None,
    *, planner_profile: CapabilityProfile | None = None,
) -> list[FeatureReadiness]: ...
```

### Component 4: dashboard Capability surface

`_build_profile_view` (exists) extends to include the liveness
line, freshness/stale flags, and the feature-readiness rows (from
`reconcile`). `alias_page.html` reshapes the four cards into the
one Capability section with the two cost-tiered action buttons;
eval per-case detail moves behind a disclosure.

### Component 5: boot warning

`app.py` startup: for each alias with enabled features, load its
profile (if any), `reconcile`, and log an ERROR-level warning per
`unsatisfied` feature (alias, feature, gap). Never raises.

## Data Models

- `CapabilityProfile`, `MeasuredGrade`, `DeclaredFact`,
  `ResourceUsage` — unchanged (Phase 12 task 24).
- `FeatureRequirement`, `FeatureReadiness`, `ReadinessStatus` —
  new (Component 3).

## Correctness Properties

### Property 1: Cheap action stays cheap
*For any* invocation of "check alive", no eval suite and no
profile run is dispatched; only reachability + the probe.
**Validates: Requirements 2.1, 2.3**

### Property 2: Profile run is visible to the dashboard
*For any* `POST /v1/profile/<alias>` run, the written
`<alias>-profile.json` is under the same `$FITT_HOME` the
dashboard reads, so the Capability surface shows it without a
path mismatch.
**Validates: Requirements 1.3**

### Property 3: Reconciler totality + three-state
*For any* (enabled features, profile), `reconcile` returns
exactly one status per enabled feature, and a feature whose
required dimension is absent from the profile is `unknown`, never
`unsatisfied`.
**Validates: Requirements 5.2, 5.6**

### Property 4: Reconciler never drives
*For any* reconciler output, no feature flag in config is mutated
and no runtime behavior changes; the only effects are the
rendered view and the boot log line.
**Validates: Requirements 5.5**

### Property 5: Producer parity
*For any* alias, the profile produced via `POST /v1/profile` and
via `fitt profile alias` is structurally equivalent (same
producer).
**Validates: Requirements 1.5, 7.3**

## Testing Strategy

- **`test_profile_runner.py`** — the extracted producer builds a
  profile with the expected dimensions from faked suite/election
  results; CLI and endpoint share it (Property 5).
- **`test_profile_endpoint.py`** — shape, auth, 404, writes under
  the gateway `$FITT_HOME` (Property 2).
- **`test_capability_reconcile.py`** — totality + three-state
  (Property 3), `unknown` on missing dimension, planner_alias
  borrow for planning, never-mutates (Property 4); hypothesis
  property for totality over random feature/profile combos.
- **`test_dashboard_views.py`** (extend) — the Capability surface
  renders liveness + declared + measured + readiness; freshness/
  stale; eval detail behind disclosure; the two action buttons.
- **`test_app.py`** (extend) — boot warning fires for an
  enabled-but-unsatisfied feature; silent when satisfied/unknown;
  never raises.
- **Cheap-action isolation test** — invoking "check alive" issues
  no suite/profile dispatch (Property 1).
- Existing probe/eval/reachability/`fitt profile` tests stay
  green except where layout intentionally moves content.

## Security

- `POST /v1/profile/<alias>` bearer-gated like `/v1/eval` and
  `/v1/probe`. No new outbound surface (same backends the eval
  already hits).
- The reconciler reads config + profile only; no secret exposure.
- Profile runs are operator-triggered and auto-approve only the
  planner's `todowrite` (the deny list still applies), same as
  the CLI producer and the scenario runner.

## Future Extensions (explicit non-goals here)

- **More profile dimensions** (synthesis, context-tolerance,
  VRAM, token-cost, refusal-rate, variance) — pulled in by the
  reconciler's demand order as features need them. VRAM is also
  the answer to the recurring 12 GB-fit question.
- **A `fitt doctor` CLI** mirroring the feature-readiness view
  for a terminal "is my setup sane" check — useful for the
  shareable end-user story, but the dashboard surface is primary
  here.
- **Auto-recommended config edits** (e.g. propose a
  `planner_alias` for a 0%-election executor) — strictly a
  suggestion surface; never auto-applied (Principle 12).
- **Folding the probe into the profile as a `liveness` dimension**
  — the broader "profile as single source of truth" end-state;
  this phase keeps the probe as the cheap tier and the profile as
  the graded tier, displayed together but run separately.
