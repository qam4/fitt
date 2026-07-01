# Design: FITT Phase 12.6 — Eval Over the Real Registry

## Overview

Make the default and realistic eval suites offer the **real registered
tool schemas in the real wire shape**, sourced from the live
`ToolRegistry`, instead of hand-written flat lookalikes. Keep the coding
suite synthetic. Thread the registry through every production caller
(endpoint, dashboard, CLI, profiler). Treat the resulting pass-rate shift
as a deliberate re-baseline.

The change is small in surface (one injection point in `run_eval_case`)
but sensitive in effect (it moves a measurement instrument, including the
capability profiler's grades), which is why it earns a spec.

## Background: the two gaps

Today `run_eval_case` does:

```python
request_body = {"messages": [...], "tools": case.tools, ...}
```

where `case.tools` is a list of flat dicts:

```python
{"name": "read_file", "description": "...", "parameters": {...}}
```

Two mismatches versus production:

1. **Content** — a lookalike, not the shipped `read_file` schema. A
   registry defect (the `cron_add` naming fumble) can't surface.
2. **Shape** — flat. The chat / executor / scenario paths all offer
   `[t.to_openai_schema() for t in registry.list_all()]`, i.e. nested
   `{"type":"function","function":{"name","description","parameters"}}`.
   The eval has been measuring a shape the live path never sends.

So the eval's "can this model tool-call" answer has been about a
different request than the one production makes.

## Key Design Decisions

### Decision 1: `EvalCase.tool_names` + a resolver, additive

Add an optional field to `EvalCase`:

```python
tool_names: tuple[str, ...] = ()   # real registry tool names to offer
```

and a pure resolver:

```python
def resolve_case_tools(
    case: EvalCase, registry: ToolRegistry | None
) -> list[dict[str, Any]]:
    """Real nested schemas when a registry + tool_names are present;
    the case's embedded (flat lookalike) tools otherwise."""
```

Resolution rules (Req 1, 2, 3):
- No registry, or no `tool_names` → return `case.tools` (unchanged; the
  backward-compatible path for existing callers/tests and the coding
  suite).
- Registry + `tool_names` → for each name, `registry.lookup(name).
  to_openai_schema()`; on `KeyError`, fall back to the embedded schema
  for that name if the case still carries one, else omit (Req 1.3, the
  graceful-degrade rule — e.g. web_search absent when the web backend
  isn't configured).

Additive: the resolver defaults to today's behavior, so nothing changes
until a case sets `tool_names` AND a caller passes a registry.

### Decision 2: The real wire shape is the correct shape

`resolve_case_tools` uses `Tool.to_openai_schema()` verbatim, so the eval
offers exactly the nested shape chat injects (Req 2). We do NOT normalise
the flat lookalikes into nested — the lookalikes stay flat as the
fallback, and the whole point is to offer the *real* object. This closes
the shape gap for the registry-sourced suites; the coding suite keeps its
flat synthetic schemas (Decision 4).

### Decision 3: Thread an optional `registry` through the runners

`run_eval_case`, `run_eval_suite`, `run_eval_case_multi`, and
`run_eval_suite_multi` each gain `registry: ToolRegistry | None = None`.
Only `run_eval_case` consumes it (via `resolve_case_tools`); the others
forward it. Optional-with-`None`-default keeps every existing call site
compiling and behaving identically until updated.

### Decision 4: Coding suite stays synthetic — deliberately

The coding suite models an *external* coding-agent (router-mode) that
offers read/edit/glob and a generic `shell` — a toolset FITT doesn't
register (there's no FITT tool named `shell`; FITT has `project_shell` /
`run_tests`). Its purpose is "does the model tool-call under a
coding-agent prompt," not "does it call FITT's tools." So its cases keep
`tool_names = ()` and their embedded schemas, and the resolver leaves
them untouched (Req 4). Only default + realistic — which already name
FITT tools (read_file, grep_repo, list_capabilities, web_search) — switch.

### Decision 5: Wire every caller; the profiler is the sensitive one

- `/v1/eval/<alias>`, dashboard `_action_run_eval`, `fitt eval alias`:
  pass `app.state.tool_registry` (or the CLI's wired registry).
- **Profiler** (`profile_runner`): it runs the realistic + coding suites
  for its tool-calling grade. Passing the registry makes that grade
  reflect the real tools — the intended fidelity gain, and the reason the
  numbers move (Req 5.4, Req 6).

### Decision 6: Re-baseline, don't alarm

The switch is landed with a fresh profiler baseline capture, so the
profiler's diff (which flags >10-point pass-rate drops) compares against
the *new* baseline, not the pre-switch one — the one-time shift is not a
regression (Req 6.1, 6.2). The observed movement (which grades changed,
roughly how much, on which model) is recorded in observed-issues
(Req 6.3). If a real tool's schema turns out to be genuinely harder for a
model than its lookalike was, that's a true finding that feeds the
tool-ergonomics backlog — not something to hide.

## Architecture

```
        EvalCase(prompt, tool_names=("read_file", ...), tools=[<fallback>])
                                   │
                    resolve_case_tools(case, registry)
                    ├─ registry + tool_names → [Tool.to_openai_schema() ...]  (real, nested)
                    └─ else                  → case.tools                     (embedded, flat)
                                   │
        run_eval_case(case, alias, router, *, registry=None) ── request_body["tools"]
                                   │  (forwarded, unchanged consumers)
        run_eval_suite / _multi (…, registry=None)
                                   ▲
        callers pass the live registry:
          /v1/eval endpoint · dashboard run-eval action · fitt eval alias · profile_runner
```

## Components and Interfaces

- `alias_eval.EvalCase` — gains `tool_names: tuple[str, ...] = ()`.
  Keep `tools` (now the fallback / coding-suite path).
- `alias_eval.resolve_case_tools(case, registry) -> list[dict]` — new
  pure helper.
- `alias_eval.run_eval_case(..., registry=None)` — resolves tools via the
  helper.
- `alias_eval.run_eval_suite / run_eval_case_multi / run_eval_suite_multi
  (..., registry=None)` — forward the registry.
- `alias_eval.default_cases()` / `realistic_cases()` — set `tool_names`;
  keep embedded `tools` as the no-registry fallback.
- `alias_eval_coding.default_coding_cases()` — unchanged.
- Callers: `eval_endpoint`, `dashboard.views._action_run_eval`,
  `cli` eval command, `profile_runner` — pass the registry.

## Data Models

- `EvalCase` gains one optional field; no other model changes.
- No change to `CaseResult`, `MultiSampleResult`, `EvalReport`, or the
  profiler's `CapabilityProfile`.

## Correctness Properties

### Property 1: Additive backward-compatibility
*For any* case with `tool_names = ()` OR any run with `registry=None`,
`resolve_case_tools` returns `case.tools` unchanged, and the offered
`tools` array is identical to today's.
**Validates: Req 1.4, 7.4**

### Property 2: Real-shape fidelity
*For any* case with `tool_names` and a registry containing those tools,
the offered `tools` array equals `[registry.lookup(n).to_openai_schema()
for n in tool_names]` — i.e. exactly what the chat handler would inject
for the same tools.
**Validates: Req 2.1, 2.2**

### Property 3: Graceful degrade on a missing tool
*For any* `tool_names` containing a name absent from the registry,
`resolve_case_tools` does not raise; it uses the embedded fallback for
that name if present, else omits it.
**Validates: Req 1.3**

### Property 4: Classification unchanged
*For any* dispatch result, the status assigned by `run_eval_case` depends
only on the response (tool_calls / finish_reason / reply), never on
whether tools came from the registry or the embedded list.
**Validates: Req 7.1**

## Testing Strategy

- **`test_alias_eval.py`** (extend):
  - `resolve_case_tools`: registry+names → nested real schemas
    (Property 2); no registry or no names → embedded (Property 1);
    missing name → fallback/omit, no raise (Property 3).
  - `run_eval_case` with a fake router that captures `request_body`:
    assert the offered `tools` are the registry's nested schemas when a
    registry is passed, and the embedded flat ones when not.
  - Existing cases still classify identically (Property 4).
- **Endpoint / dashboard / CLI / profiler tests**: assert the registry is
  threaded (the offered tools are real) without a live model — a fake
  router capturing the body, or asserting the wiring call.
- Existing eval tests that pass no registry stay green (Property 1).
- **Re-baseline**: a manual profiler run on the hub (recorded in
  observed-issues), not a unit test.

## Security

No new surface. Same tools the chat path already offers the same model;
the eval just stops offering a divergent copy. No secret exposure (tool
schemas carry no secrets).

## Future Extensions (non-goals here)

- Retiring the embedded lookalike `tools` entirely once every caller
  threads a registry (a later cleanup; kept now as the fallback).
- A registry-sourced *coding* suite (only if FITT grows a real
  coding-agent toolset; today the synthetic set is correct).
- Auto-generating cases from the registry (over-fits the harness to the
  inventory; the ladder tests the model with representatives — see
  project-overview "measurement ladder").
