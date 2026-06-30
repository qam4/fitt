# Requirements Document

FITT Phase 12.5 — Capability Surface

## Introduction

Phase 12 task 24 built a per-alias capability profile (declared
facts + measured grades), and Stage 1 (2026-06-30) added a
`plan-election` dimension. But the profiler is **CLI-only**
(`fitt profile alias`), while the probe and the eval suites are
already runnable from the dashboard — so the one measurement an
operator most wants to *see* is the one they can't *run* without
a terminal. Worse, the alias page now shows four overlapping
measurement surfaces (probe + three eval suites + profile), and
the profiler re-runs the same realistic/coding suites the evals
already ran. For the author it's confusing; for an end user it's
a wall of buttons with no clear "what can this model do, and what
should I turn on" answer.

This phase consolidates the **display** into one Capability
surface on the existing per-alias page (Phase 7.6), while keeping
the **actions tiered by cost** so a cheap "is it alive?" check
never drags in a multi-minute eval+profile run. It makes the
profile runnable from the dashboard (writing where the dashboard
reads), and adds the feature↔capability **reconciler** — the
recommendation layer that turns the profile's facts into "enable
planning on this alias / don't, it can't" (roadmap Principle 12).

Derived from the design in `design.md` (design-first workflow),
and from the recurring "how does benchmarking inform config"
thread that produced roadmap Principle 12 and the BACKLOG
reconciler entry.

## Glossary

- **Reachability check**: the cheap, no-inference ping
  (`GET /api/tags` for Ollama, `/v1/models` for cloud) extracted
  to `reachability.check_reachable` in Phase 7.6. Answers "is the
  host up?"
- **Probe**: the one-canary tool-call (`alias_probe`). Answers
  "can it emit `tool_calls` at all?" Cheap (one inference).
- **Eval suite**: a multi-case, multi-sample run
  (`default` / `coding` / `realistic`) producing graded
  reliability. The measurement *engine*. Minutes.
- **Profile**: the per-alias aggregation
  (`<alias>-profile.{md,json}`) of declared facts + measured
  grades + cost + resources, with a baseline diff. The durable
  "what can this model do" record and rendered surface.
- **Capability surface**: the consolidated section on
  `/dashboard/alias/<id>` that displays reachability + probe +
  declared facts + measured grades + feature readiness, with
  cost-tiered actions.
- **Reconciler**: the pure layer that, given an alias's enabled
  features and its profile, reports each feature as
  `satisfied` / `unsatisfied` / `unknown`. The recommendation
  engine (Principle 12); surfaces, never auto-drives.
- **Cost tier**: the rung on the reachability → probe → eval →
  profile ladder, each strictly more expensive than the last.

## Requirements

### Requirement 1: Run the profile from the dashboard

**User Story:** As an operator who works in the dashboard, I want
to trigger a capability profile for an alias from the UI, so that
I never need the CLI to populate or refresh the Capability card.

#### Acceptance Criteria

1. THE system SHALL expose `POST /v1/profile/<alias>` that runs
   the profiler for one alias and returns the resulting profile
   as JSON, bearer-gated, mirroring `/v1/eval/<alias>` and
   `/v1/probe/<alias>`.
2. THE per-alias dashboard page SHALL provide an action that
   calls this endpoint and refreshes the Capability surface.
3. WHEN the profiler runs via the endpoint, it SHALL write
   `<alias>-profile.{md,json}` under the **gateway process's**
   `$FITT_HOME/eval/`, i.e. the same directory the dashboard
   reads — so a profile run is always visible to the dashboard
   that triggered it (no host-vs-container `FITT_HOME` mismatch).
4. WHEN the alias is unknown, THE endpoint SHALL return 404 with
   a typed error envelope.
5. THE profiler producer logic SHALL be extracted from the CLI
   command into a reusable function called by BOTH the CLI and
   the endpoint, so the two paths cannot drift.
6. THE existing `fitt profile alias` CLI SHALL continue to work,
   producing the same output via the shared producer.

### Requirement 2: Cost-tiered actions (cheap liveness ≠ full measure)

**User Story:** As an operator, I want to check whether a model
is alive and reachable without paying for a full eval+profile
run, so that a routine "is it up?" check stays a few seconds.

#### Acceptance Criteria

1. THE Capability surface SHALL offer a cheap action ("check
   alive") that runs ONLY the reachability check and the probe
   (no eval suites, no profile), completing in seconds.
2. THE Capability surface SHALL offer a separate, explicitly
   heavier action ("measure capability") that runs the eval
   engine + plan-election and refreshes the profile grades.
3. THE two actions SHALL be independent: invoking the cheap
   action SHALL NOT trigger any eval suite or profile run, and
   invoking the heavy action SHALL be clearly labelled as the
   minutes-long one.
4. THE UI SHALL communicate the relative cost of each action
   (e.g. an estimated-duration or "slow" affordance on the
   measure action), so the operator chooses knowingly.

### Requirement 3: One consolidated Capability surface (display)

**User Story:** As an operator (and a prospective end user), I
want one section that tells me what a model can do, instead of
four overlapping cards, so that the page is scannable and not
intimidating.

#### Acceptance Criteria

1. THE per-alias page SHALL present a single Capability surface
   containing: a reachability/probe liveness line, declared facts
   (context window, tools/thinking/vision, size), measured grades
   (tool-calling, coding, plan-election, ...) with capability AND
   cost side by side, resources, and (Requirement 5) feature
   readiness.
2. THE three eval suites SHALL NOT appear as three independent
   top-level cards; they SHALL be presented as the engine behind
   the "measure capability" action, with their detailed results
   reachable from within the Capability surface.
3. AN advanced affordance MAY still trigger a single eval suite,
   but it SHALL NOT be the page's headline.
4. NO measurement information currently shown (probe dimensions,
   eval per-case detail, profile grades) SHALL be lost in the
   consolidation; it MAY move location or sit behind a disclosure.

### Requirement 4: Per-dimension freshness

**User Story:** As an operator, I want to see when each piece of
the Capability surface was last measured, so that I can tell
current data from stale and decide whether to re-measure.

#### Acceptance Criteria

1. THE Capability surface SHALL display a "last measured"
   timestamp for the profile (and for the probe/reachability
   liveness line).
2. WHERE a measurement is older than a configurable/sensible
   threshold, THE surface SHALL flag it as stale rather than
   presenting it as current.
3. THE cheap liveness check and the heavy measure SHALL carry
   independent timestamps, since they run at different cadences.

### Requirement 5: Feature↔capability reconciler (the recommendation)

**User Story:** As an operator, I want the surface to tell me
which FITT features the bound model can actually drive and which
I've enabled that it can't, so that I don't ship a silently dead
feature (orchestration on a model that elects to plan 0% of the
time).

#### Acceptance Criteria

1. THE system SHALL define a model-agnostic feature→required-
   capability map keyed on profile **dimensions** (never model
   names): e.g. planning requires `plan-election` on the alias
   or its `planner_alias`.
2. THE system SHALL provide a pure reconciler that, given an
   alias's enabled features and its profile, returns per feature
   exactly one of: `satisfied`, `unsatisfied`, `unknown`
   (dimension not measured).
3. THE Capability surface SHALL render the reconciler output as a
   "feature readiness" view; `unknown` SHALL point the operator
   at the "measure capability" action.
4. AT gateway boot, WHERE an enabled feature is `unsatisfied` for
   its alias, THE system SHALL log a warning naming the alias,
   feature, and the measured gap — without refusing to start
   (fail-loud, not fail-closed; mirrors `check_missing_api_keys`).
5. THE reconciler SHALL surface/recommend only; it SHALL NEVER
   auto-enable or auto-disable a feature (the operator-in-the-loop
   commitment; measurements are sample-noisy).
6. WHERE a feature's required dimension isn't measured for an
   alias, the reconciler SHALL report `unknown` (not a
   false-negative `unsatisfied`).

### Requirement 6: Measure reuses, doesn't redundantly re-run

**User Story:** As an operator, I don't want the profile to
re-run the same eval suites the evals already ran, so that a
"measure capability" pass isn't doing duplicate work.

#### Acceptance Criteria

1. THE profiler SHALL treat the eval suites as its measurement
   engine such that one "measure capability" action runs each
   needed suite once and the profile aggregates those results
   (rather than the eval action and the profile action each
   running the suites separately).
2. THE eval suite results and the profile grades SHALL derive
   from a single run per measure action (no double-dispatch of
   the same suite within one measure).
3. This consolidation SHALL NOT change the meaning of any
   existing eval status or profile grade.

### Requirement 7: No regression in existing surfaces

**User Story:** As FITT's developer, I want the probe, eval, and
reachability behavior to be unchanged except where this phase
explicitly consolidates them, so that the refactor surfaces
information without altering live behavior.

#### Acceptance Criteria

1. THE `/v1/eval/<alias>` and `/v1/probe/<alias>` endpoints SHALL
   keep their existing contracts.
2. THE existing probe/eval/reachability tests SHALL pass
   (adjusted only where the dashboard layout intentionally moves
   content).
3. THE CLI `fitt profile alias` SHALL produce output equivalent
   to today's via the extracted shared producer.
