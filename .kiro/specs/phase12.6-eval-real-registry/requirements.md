# Requirements Document

FITT Phase 12.6 — Eval Over the Real Registry

## Introduction

The eval suites (Phase 4.11, extended in 12/12.5) measure whether a bound
model can tool-call, and the capability profiler grades tool-calling from
those suites. But every case hand-writes its own tool schema — a
*lookalike* of a real FITT tool — so the suites have never exercised the
tools the gateway actually ships. Two consequences, both discovered while
shipping the tool-consistency lint (2026-07-01):

1. **Wrong content.** A schema-ergonomics bug in the shipped registry
   (the `cron_add` `message` vs `text` fumble) is invisible to the eval
   by construction, because the eval offers its own clean copy.
2. **Wrong shape.** The lookalikes are flat `{"name", "description",
   "parameters"}` dicts. The real chat / executor / scenario paths offer
   tools in the OpenAI *nested* shape (`{"type":"function","function":
   {...}}`, via `Tool.to_openai_schema()`). So the eval has been
   measuring a wire shape the live path never uses.

This phase makes the **default** and **realistic** suites offer the
**real registered tool schemas**, in the **real wire shape**, so the eval
measures what production actually sends the model. This is the "feed the
eval real tool forms" lane (b-complement of the measurement-ladder split
in project-overview steering); the separate offline tool-consistency lint
already shipped.

Because this changes what the model is shown, measured pass-rates —
including the capability profiler's tool-calling grades — are expected to
shift. That re-baseline is the point (faithful measurement), not a
regression to suppress.

## Glossary

- **Eval case** (`EvalCase`): one curated prompt + expected tool-call
  shape. Today carries an embedded `tools` list (flat lookalike schemas).
- **Lookalike schema**: a hand-written tool schema in a case, a copy of a
  real tool, in the flat `{name, description, parameters}` shape.
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

### Requirement 2: Tools are offered in the real wire shape

**User Story:** As FITT's developer, I want the eval to offer tools in the
same OpenAI nested shape the live chat path uses, so that the eval
measures the real request the model receives.

#### Acceptance Criteria

1. WHEN a case's tools are sourced from the registry, THE offered `tools`
   array SHALL use the nested `{"type":"function","function":{...}}` shape
   produced by `Tool.to_openai_schema()`.
2. THE offered shape SHALL match, field for field, what the chat handler
   injects for the same tool (no eval-only shape divergence).

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
