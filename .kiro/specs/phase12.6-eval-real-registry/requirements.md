# Requirements Document

FITT Phase 12.6 — Eval Over the Real Registry

## Introduction

The eval suites (Phase 4.11, extended in 12/12.5) measure whether a bound
model can tool-call, and the capability profiler grades tool-calling from
those suites. But every case hand-writes its own tool schema — a
*lookalike* of a real FITT tool — so the suites have never exercised the
tools the gateway actually ships.

**The gap is content / source-of-truth, not wire shape.** (An earlier
scoping note claimed the lookalikes were a *flat* shape the live path
never sends; that was wrong — the embedded schemas are already nested
`{"type":"function","function":{...}}`, the same shape
`Tool.to_openai_schema()` produces. Corrected 2026-07-01.) The real
problem is that the eval offers hand-written *copies*, so:

1. **Drift is uncaught.** If a shipped tool's schema changes (or was
   always subtly different from its lookalike), the eval keeps testing
   the stale copy and never notices — the opposite of what a
   regression-catcher should do.
2. **Defects in the shipped shape are invisible.** A schema-ergonomics
   problem in the real registry (the `cron_add` `message` vs `text`
   fumble class) can never surface in a suite that offers its own clean
   copy — and a future case for such a tool would test the copy, not the
   ship.

This phase makes the **default** and **realistic** suites offer the
**real registered tool schemas** (sourced from the live `ToolRegistry`
via `Tool.to_openai_schema()`), so the eval measures the exact tools
production sends the model and drift is caught. This is the "feed the
eval real tool forms" lane (b-complement of the measurement-ladder split
in project-overview steering); the separate offline tool-consistency lint
already shipped.

Because this changes the offered schemas, measured pass-rates — including
the capability profiler's tool-calling grades — **may shift**. The
default/realistic lookalikes are already close to the real schemas (same
nested shape, similar params), so the shift is expected to be modest; but
any shift is a deliberate re-baseline (faithful measurement), not a
regression to suppress. If a real tool proves genuinely harder for a model
than its lookalike was, that's a true tool-ergonomics finding.

## Glossary

- **Eval case** (`EvalCase`): one curated prompt + expected tool-call
  shape. Today carries an embedded `tools` list (lookalike schemas).
- **Lookalike schema**: a hand-written tool schema in a case, a copy of a
  real tool, in the same nested `{type, function}` shape the real tool
  uses — a copy, not the live object.
- **Real schema**: the schema the gateway actually offers the model,
  `Tool.to_openai_schema()` (nested `{type, function}`), sourced from the
  live `ToolRegistry`.
- **Tool registry** (`ToolRegistry`): the live set of registered tools
  (`list_all()`, `lookup(name)`), including inline, MCP, and skill tools.
- **Default / realistic / coding suites**: the three eval suites.
  Default = FITT's own tools, minimal prompt. Realistic = default cases
  under FITT's live system prompt. Coding = a synthetic *external*
  coding-agent toolset (read/edit/glob/shell) under a coding-agent
  prompt — deliberately NOT FITT's registry.
- **Re-baseline**: capturing the new measured pass-rates after the switch
  as the new known-good, rather than treating the shift as a regression.

## Requirements

### Requirement 1: Cases can reference real registered tools by name

**User Story:** As FITT's developer, I want an eval case to name the tools
it offers rather than embed a hand-written schema, so that the model sees
the exact tool the gateway ships.

#### Acceptance Criteria

1. THE `EvalCase` SHALL support an optional list of tool names that
   identify tools to offer from the live registry.
2. WHERE a case names tools AND a registry is provided, THE eval SHALL
   offer those tools' real schemas (`Tool.to_openai_schema()`).
3. WHERE a named tool is not present in the registry, THE eval SHALL
   degrade gracefully (fall back to the case's embedded schema for that
   tool if present, else omit it) rather than raising.
4. THE mechanism SHALL be additive: a case with no tool names, or a run
   with no registry, SHALL behave exactly as today (embedded lookalikes).

### Requirement 2: Tools come from the live registry (single source of truth)

**User Story:** As FITT's developer, I want the eval to offer the exact
tool object the gateway ships, so that the eval measures the real request
the model receives and drift between a copy and the shipped tool is
caught.

#### Acceptance Criteria

1. WHEN a case's tools are sourced from the registry, THE offered `tools`
   array SHALL be `Tool.to_openai_schema()` for each named tool — the same
   object the chat handler injects.
2. THE offered schema SHALL match, field for field, what the chat handler
   injects for the same tool (no eval-only divergence in content or
   shape).

### Requirement 3: Default and realistic suites use the real registry

**User Story:** As an operator, I want the default and realistic suites to
test FITT's real tools, so that a shipped tool's shape defect shows up in
the eval.

#### Acceptance Criteria

1. THE default suite's cases SHALL reference real FITT tools by name
   (e.g. read_file, grep_repo, list_capabilities).
2. THE realistic suite SHALL likewise reference real tools (including
   web_search for the live-fact case).
3. THE case prompts SHALL remain valid against the real schemas (e.g. a
   prompt that names a project and a path, for a tool whose real schema
   requires `project` + `path`).

### Requirement 4: The coding suite stays synthetic

**User Story:** As FITT's developer, I want the coding suite to keep
modelling an external coding-agent toolset, so that its router-mode
measurement is unchanged.

#### Acceptance Criteria

1. THE coding suite SHALL continue to offer its synthetic coding-agent
   toolset (including tools not in FITT's registry, such as a generic
   `shell`) via embedded schemas.
2. THIS phase SHALL NOT change the coding suite's cases or results.

### Requirement 5: Registry threaded through every production caller

**User Story:** As an operator, I want every way I run the eval to use the
real tools, so that the dashboard, endpoint, CLI, and profiler all agree.

#### Acceptance Criteria

1. THE `/v1/eval/<alias>` endpoint SHALL pass the live registry to the
   suite runner.
2. THE dashboard "run eval" action SHALL pass the live registry.
3. THE `fitt eval alias` CLI SHALL pass the live registry.
4. THE capability profiler (which runs the realistic + coding suites for
   its tool-calling grade) SHALL pass the live registry so its grades
   reflect the real tools.

### Requirement 6: The re-baseline is captured, not alarmed

**User Story:** As an operator, I don't want the switch to read as a
capability regression, so that I can tell "we changed the measurement"
from "the model got worse."

#### Acceptance Criteria

1. THE change that switches a suite to real tools SHALL be landed together
   with a fresh baseline capture (the new pass-rates recorded as the new
   known-good).
2. THE profiler's baseline-diff SHALL NOT flag the one-time switch as a
   regression after the re-baseline (the diff is against the new baseline).
3. THE observed re-baseline (which grades moved, and roughly how much)
   SHALL be recorded in `docs/observed-issues.md`.

### Requirement 7: No regression in unrelated eval behavior

**User Story:** As FITT's developer, I want the classification,
multi-sampling, reporting, and transient-failure handling to be unchanged,
so that only the *offered tools* change.

#### Acceptance Criteria

1. THE case classification (pass / wrong_tool / narrated / dispatch-
   failure taxonomy) SHALL be unchanged.
2. THE multi-sample aggregation and transient-exclusion SHALL be
   unchanged.
3. THE report rendering + JSON sidecar SHALL be unchanged.
4. THE existing eval tests that don't opt into a registry SHALL remain
   green.
