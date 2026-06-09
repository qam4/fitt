# Tasks: FITT Phase 12 — Planning & Execution

Sequencing principles:
- **Get a real model in the loop first (12a).** The model-sensitive
  parts — the planner prompt, recovery nudges, capability profiling —
  cannot be built with confidence against fakes; a fake only returns
  what you tell it. 12a removes that blindness by wiring the dev/eval
  harness to a real model. It is an enabling step, not a baseline
  ritual.
- **Build vs validate.** The plumbing (PromptResolver, PlanStore,
  orchestrator, recovery *triggers*, events) is pure logic, fully
  unit-testable with fakes, and builds normally regardless of model
  access. The prompt *content* and "does it actually work on a real
  weak model" require 12a's real-model loop.
- **Each sub-phase leaves something usable** (Principle 4).
- **Smallest measurable slice first.** The core hypothesis (elected
  planning makes a weak model competent on a multi-step turn) is
  testable after 12c, before the heavier 12e.
- **Phase 8 (compaction) gates only 12e.** 12a-12d and the core of
  12f run without it (short multi-step turns don't overflow), so the
  hypothesis can be tested before Phase 8 lands.

Numbers are continuous so tests/commits can reference them. Mark
`[x]` as completed; don't delete completed tasks. Story/criteria
references point at `requirements.md`; property refs (Cn) at
`design.md`.

## Phase 12a — Real model in the loop (de-blind the dev workflow)

- [x] 1. Wire the dev/eval harness to a real model backend: local
  Ollama (`localhost:11434`) and/or the EC2 GPU box. Pull a couple of
  representative weak models (e.g. `qwen3:8b` plus the bound model).
  Model-agnostic — no hardcoded model names; the backend/models are
  config.
- [ ] 2. Establish real-model test conventions given non-determinism:
  assert on **structure/properties**, never exact strings;
  `temperature=0` + fixed seed for reproducible dev runs; **multi-
  sample** (k runs → pass rate) for behavioral signal.
- [ ] 3. Record/replay: capture real model responses to cases once and
  replay them deterministically so CI exercises the plumbing against
  real output *shapes* without a live model or non-deterministic flake
  (ref OpenCode `http-recorder` / `recorded-*`).
- [ ] 4. With the real-model loop in place, run the **current flat
  loop** on a multi-step `daily_news_summary` case (real registered
  tools) against a real model and read the actual failure. Not a
  ceremonial baseline — the real-output read that informs the planner
  prompt (Story 7.1, 7.2, 7.4).

## Phase 12b — Plan artifact + prompt resolution (the spine)

- [x] 5. `PromptResolver.resolve(step, alias)` with per-step defaults
  for `plan|execute|compact|recover` and optional per-alias overrides;
  config-driven, no code change to override (Story 2.1-2.5).
- [ ] 6. `PlanStore`: durable structured plan (markdown todo),
  persist + re-inject, hydrate-from-history (fresh-agent-per-turn).
  Structured round-trip, no lossy summary (Story 1.2, 1.4; property
  C5).
- [ ] 7. A `todowrite`-style plan tool offered in the planner pass;
  writes to `PlanStore`.
- [ ] 8. Planner pass: `run_agent_loop` with the `plan`-step prompt +
  plan tool, elected (prompt-nudged), model self-elects (Story 1.1,
  1.3).
- [ ] 9. Executor pass: `run_agent_loop` with the `execute`-step
  prompt and the plan re-injected from `PlanStore`; ticks todos as
  steps complete (Story 3.1; property C1).
- [ ] 10. Orchestrator sequencing plan -> execute for one turn; single
  entry point that the chat handler and cron runner call (as they
  call `run_agent_loop` today).
- [ ] 11. Make the iteration budget configurable per alias (replaces
  the hard-coded 10), higher default for planned turns (Story 3.3).
- [ ] 12. Unit tests (fakes): resolver precedence, PlanStore
  round-trip (hypothesis, C5), planner-elects-on-multistep, executor
  re-injects plan (C1).

## Phase 12c — Recovery (make elected safe)

- [ ] 13. Ground-truth trouble detector: tool error, identical
  retried call, empty-after-tools, N iterations with zero successful
  calls, budget exhausted. Facts only — no prose-shape/intent
  inference (Story 5.1, 5.2; property C4).
- [ ] 14. Recovery actions, escalating: continue-nudge (Hermes's
  empty-after-tools pattern) -> repair malformed tool call -> re-plan
  on a **clean context** (Story 5.3) -> honest stop (Story 4.1, 4.2).
- [ ] 15. Capability-gap ("I'd need a tool to X") is a terminal
  honest outcome, distinct from thrash; not retried/escalated (Story
  4.4). No path rebinds to a cloud alias (Story 4.3; property C6).
- [ ] 16. Tests: each trigger classified on synthetic + recorded
  transcripts; recovery never fires on a clean all-success transcript
  (C4 negative); a skip-then-stumble turn self-corrects via re-plan.

## Phase 12d — Visibility

- [ ] 17. Emit plan-created, step-start/complete, and re-plan events
  to the Phase 7 turn-event stream (Story 6.1, 6.2).
- [ ] 18. Render the plan + step progress in the existing live-turn
  renderer and the dashboard turn-detail page; no new viewer (Story
  6.3).

## Phase 12e — Compaction integration (depends on Phase 8)

- [ ] 19. Wire the compaction adapter into the executor pass: fire on
  context overflow, structured anchor preserves plan/progress/next
  (Story 3.2).
- [ ] 20. Guarantee the active plan survives compaction by re-injecting
  it from `PlanStore` (property C3) — the plan can never be compacted
  out.
- [ ] 21. E2E test: a cron firing of `daily_news_summary` with a
  compaction triggered mid-run still delivers via `send_message`;
  assert plan survived (C3).

## Phase 12f — Eval comparison + capability profile + close-out

- [ ] 22. Flat-loop vs planned comparison on the same alias for the
  news case (Story 7.3) — the headline result: did planning beat the
  12a flat-loop read? Multi-sample (pass rate), not single-shot.
- [ ] 23. Per-alias under-plan detection: does the alias skip planning
  on a turn that needed it (Story 7.5)? Feeds per-alias planner-prompt
  tuning (Story 2.4).
- [ ] 24. Thin capability profile: measure a small set of **per-
  dimension** grades — tool-calling reliability (and at what prompt
  size it degrades), plans-when-nudged, context tolerance — across the
  model roster. Per-dimension, **not** a single scalar tier. The
  profile **informs/surfaces** recommended config to the operator; it
  does **not** silently auto-drive behavior in v1. Cases drawn from
  real usage to avoid overfitting the harness to the benchmark.
  Behavior keys off measured capability, never model names (the
  model-agnostic guarantee). Keep it to the dimensions that change a
  harness decision now; the full profiler is successor work.
  Context-tolerance method: the declared context window
  (Ollama/models.dev) is a free bound — it rules out prompts that can
  never fit and gives headroom (operating-point vs ceiling) at zero
  cost; the **measured operating-point pass-rate** at FITT's real
  prompt size is the actual degradation signal (declared gives the
  ceiling, only measurement finds where it breaks below it). Optional
  deeper read: binary-search a coarse threshold with a *cheap*
  structure-adherence probe (one "emit a tool_call" case, multi-
  sampled) rather than the full suite. Degradation depends on task +
  prompt content, so record a measured range, not a universal scalar.
  NOTE (2026-06-09): dev runs only reached ~970 tokens (capability
  block; memory/skills off) — hermes3:8b held 6/6 bare and realistic —
  so a true degradation read still needs a full production-size
  prompt.
- [ ] 25. Run the planner-on-`qwen3:14b` / executor-on-`hermes3:8b`
  experiment ("concentrate intelligence in planning") and record the
  delta — a concrete test that the harness, not the model, is the
  lever.
- [ ] 26. Live-validation pass on the hub; record outcomes in
  `docs/observed-issues.md` (including whether the
  under-harnessed-not-incapable hypothesis held).

## Deferred / successor work (not this phase)

- Subagent / many-context execution (Story 3.4) — the "split many
  contexts" cure, for genuinely parallel/independent sub-tasks.
- DAG plan representation (when parallel subagents arrive).
- A `forced` planning mode — documented fallback, built only if a
  model proves chronically unable to self-elect (design D3,
  Divergences).
- Full capability profiler / model-bucketing platform — grows from
  the thin profile in task 24 when it earns its keep.
- Speculative-parallel latency optimisation (Non-Goals).
