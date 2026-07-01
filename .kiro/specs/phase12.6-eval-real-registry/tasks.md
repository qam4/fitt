# Implementation Plan: FITT Phase 12.6 — Eval Over the Real Registry

**Status:** not started

## Overview

Make the default + realistic eval suites offer the real registered tool
schemas in the real wire shape, keep the coding suite synthetic, thread
the registry through every production caller, and re-baseline the
profiler. Additive by construction — each step keeps the tree green, and
the behavior change only activates once cases name tools AND a caller
passes a registry.

Status legend: `[x]` done, `[ ]` not yet.

## Phase 12.6a — Mechanism (additive, no behavior change yet)

- [ ] 1. Add `tool_names: tuple[str, ...] = ()` to `EvalCase` (keep the
  existing `tools` field as the fallback / coding-suite path). (Req 1.1)
- [ ] 2. Add pure `resolve_case_tools(case, registry) -> list[dict]`:
  registry+names → `[Tool.to_openai_schema() for n in tool_names]`, with
  per-name graceful fallback to the embedded schema then omit on
  `KeyError`; no registry or no names → `case.tools`. (Req 1.2, 1.3, 1.4,
  2.1, 2.2, Properties 1, 2, 3)
- [ ] 3. Thread `registry: ToolRegistry | None = None` through
  `run_eval_case` (consumes it via the resolver) and forward it through
  `run_eval_suite`, `run_eval_case_multi`, `run_eval_suite_multi`.
  (Req 1.4, Decision 3)
- [ ] 4. Tests: `resolve_case_tools` (all three branches, Properties
  1-3); `run_eval_case` with a body-capturing fake router asserts real
  nested schemas when a registry is passed vs embedded flat when not;
  classification unchanged (Property 4). ruff/mypy/pytest green.

## Phase 12.6b — Switch default + realistic to the real registry

- [ ] 5. Set `tool_names` on `default_cases()` (read_file, grep_repo,
  list_capabilities, tool_disambiguation's pair) and `realistic_cases()`
  (adds web_search); keep embedded `tools` as the no-registry fallback.
  Verify prompts remain valid against the real schemas (project+path
  etc.). (Req 3.1, 3.2, 3.3)
- [ ] 6. Leave `default_coding_cases()` untouched; add a one-line comment
  pinning why it stays synthetic (external coding-agent toolset incl.
  `shell`, not FITT's registry). (Req 4.1, 4.2)
- [ ] 7. Tests: default + realistic cases now resolve to real schemas
  under a registry; coding suite still resolves to its embedded schemas.
  ruff/mypy/pytest green; commit + push.

## Phase 12.6c — Wire every production caller

- [ ] 8. `/v1/eval/<alias>` endpoint: pass `app.state.tool_registry` to
  the suite runner. (Req 5.1)
- [ ] 9. Dashboard `_action_run_eval`: pass `request.app.state.
  tool_registry`. (Req 5.2)
- [ ] 10. `fitt eval alias` CLI: pass the wired registry. (Req 5.3)
- [ ] 11. Profiler (`profile_runner`): pass the registry into the
  realistic + coding suite runs used for the tool-calling grade.
  (Req 5.4)
- [ ] 12. Tests: each caller threads the registry (body-capture or wiring
  assertion, no live model); existing caller tests stay green (Req 7).
  ruff/mypy/pytest green; commit + push.

## Phase 12.6d — Re-baseline + close-out

- [ ] 13. On the hub, run `fitt profile alias` for the bound aliases to
  capture the fresh baselines (the switch moves grades; this records the
  new known-good so the diff isn't a false regression). (Req 6.1, 6.2)
- [ ] 14. Record the observed re-baseline in `docs/observed-issues.md`:
  which grades moved, roughly how much, on which model; note whether any
  real tool proved genuinely harder than its lookalike (a true
  tool-ergonomics finding, not noise). (Req 6.3)
- [ ] 15. BACKLOG: mark the eval-over-real-registry item shipped; update
  Now/Next. Roadmap/steering pointer only if the plan shifts.

## Verification (manual, on the hub / home box)

- [ ] V1. Run the default + realistic suites from the dashboard on a
  bound alias; confirm the offered tools are FITT's real tools (spot via
  the eval detail / a capture) and the run completes.
- [ ] V2. Confirm the coding suite results are unchanged.
- [ ] V3. Profile an alias; confirm the tool-calling grade reflects the
  real tools and the baseline diff is clean after the re-baseline.

## Definition of done

- 12.6a-12.6c complete; existing eval/endpoint/dashboard/CLI/profiler
  tests green; new tests cover Properties 1-4.
- Coding suite unchanged; default + realistic offer real nested schemas.
- Re-baseline captured and recorded (12.6d).
- Standard test/lint/typecheck cycle green in both packages.

## Notes

- **Additive first (12.6a) so the tree stays green**: the mechanism lands
  with no behavior change; the switch (12.6b) and wiring (12.6c) activate
  it. This is the smallest-safe-slice ordering.
- **The coding suite is intentionally excluded** — it models an external
  coding-agent, not FITT's tools (design Decision 4).
- **Expect the numbers to move** (design Decision 6): the switch is a
  re-baseline, not a regression. A real tool that's genuinely harder than
  its lookalike is a finding for the tool-ergonomics backlog, not noise
  to suppress.
