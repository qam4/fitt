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
- [x] 3. Record/replay: capture real model responses to cases once and
  replay them deterministically so CI exercises the plumbing against
  real output *shapes* without a live model or non-deterministic flake
  (ref OpenCode `http-recorder` / `recorded-*`). DONE: `record_replay.py`
  — `RecordingRouter` wraps a real `AliasRouter` and captures each
  dispatch to a JSON cassette; `ReplayRouter` serves a cassette with no
  live call (keyed by a volatile-field-stripped request hash, sequential
  for duplicate keys, loud `CassetteMiss` on an unrecorded request).
  Drop-in at the `dispatch`/`resolve` seam — no loop/planner/orchestrator
  change. `test_record_replay.py` proves a full plan -> execute turn
  records then replays identically with no inner router. Capturing
  cassettes from a real backend is operator-driven two ways, both
  wrapping the live router (faithful, no re-wired copy):
  `fitt eval alias <alias> --record <path>` for the single-shot eval
  suite, and `FITT_RECORD_CASSETTE=<path>` at gateway start +
  `POST /v1/internal/record-flush` to capture full multi-pass
  orchestrated turns as the live chat path runs them. Exercised when
  the real-model loop runs (12f).
- [ ] 4. With the real-model loop in place, run the **current flat
  loop** on a multi-step `daily_news_summary` case (real registered
  tools) against a real model and read the actual failure. Not a
  ceremonial baseline — the real-output read that informs the planner
  prompt (Story 7.1, 7.2, 7.4).

## Phase 12b — Plan artifact + prompt resolution (the spine)

- [x] 5. `PromptResolver.resolve(step, alias)` with per-step defaults
  for `plan|execute|compact|recover` and optional per-alias overrides;
  config-driven, no code change to override (Story 2.1-2.5).
- [x] 6. `PlanStore`: durable structured plan (markdown todo),
  persist + re-inject, hydrate-from-history (fresh-agent-per-turn).
  Structured round-trip, no lossy summary (Story 1.2, 1.4; property
  C5).
- [x] 7. A `todowrite`-style plan tool offered in the planner pass;
  writes to `PlanStore`.
- [x] 8. Planner pass: `run_agent_loop` with the `plan`-step prompt +
  plan tool, elected (prompt-nudged), model self-elects (Story 1.1,
  1.3).
- [x] 9. Executor pass: `run_agent_loop` with the `execute`-step
  prompt and the plan re-injected from `PlanStore`; ticks todos as
  steps complete (Story 3.1; property C1).
- [x] 10. Orchestrator sequencing plan -> execute for one turn; single
  entry point that the chat handler and cron runner call. DONE:
  `run_orchestrated_turn` (`orchestrator.py`) is a messages-based
  drop-in returning `AgentLoopResult`; wired into chat + cron **gated
  per alias** (`Config.is_orchestrated`, default off); PlanStore +
  `todowrite` tool + PromptResolver wired into `create_app`. Preserves
  the assembled system prompt (plan re-injected, identity kept). Full
  suite green (1534 passed).
- [x] 11. Make the iteration budget configurable per alias (replaces
  the hard-coded 10), higher default for planned turns (Story 3.3).
  DONE: `AliasOrchestrationConfig` gained `planner_alias` (Story 2.2),
  `planner_iterations`, `executor_iterations` (all optional). Orchestrator
  defaults `_DEFAULT_PLANNER_ITERATIONS=1` (plan captured on first
  `todowrite`, keeps a cloud `planner_alias` under RPM) and
  `_DEFAULT_EXECUTOR_ITERATIONS=15` (a planned turn works a multi-step
  plan). `planner_alias` runs the plan pass on a different alias than
  the executor; fail-loud if it names an unknown alias. Wired through
  chat + cron call sites. Full suite green (1543 passed).
- [x] 12. Unit tests (fakes): resolver precedence, PlanStore
  round-trip (hypothesis, C5), planner-elects-on-multistep, executor
  re-injects plan (C1). DONE incrementally across tasks 5-11; the four
  required cases live in: `test_prompt_resolver.py`
  (`test_per_alias_override_beats_default`,
  `test_global_default_override_beats_builtin_but_loses_to_alias`),
  `test_plan_store.py` (`test_plan_round_trip_property`, @given 150
  examples, + disk round-trip), `test_planner.py`
  (`test_planner_writes_plan_when_model_elects` /
  `test_planner_elects_not_to_plan`), and `test_orchestrator.py`
  (`test_orchestrator_plans_then_executes` asserts `[Plan]` re-injected
  into the execute pass's system message). 49 passed.

## Phase 12c — Recovery (make elected safe)

- [x] 13. Ground-truth trouble detector: tool error, identical
  retried call, empty-after-tools, N iterations with zero successful
  calls, budget exhausted. Facts only — no prose-shape/intent
  inference (Story 5.1, 5.2; property C4). DONE: `trouble.py`
  `detect_trouble(status, tool_calls, assistant_text)` returns a
  `Trouble(kind, detail)` over the facts an `AgentLoopResult` already
  carries (`tool_calls_for_memory` statuses, loop status, final text).
  Precedence: most-specific cause first, `budget_exhausted` is the
  catch-all. Emptiness is treated as presence-of-content, never prose
  shape (C4). `test_trouble.py`: each signal + precedence + C4-negative
  hypothesis property (clean all-success transcript never trips
  recovery). 17 passed. Actions are task 14.
- [x] 14. Recovery actions, escalating: continue-nudge (Hermes's
  empty-after-tools pattern) -> repair malformed tool call -> re-plan
  on a **clean context** (Story 5.3) -> honest stop (Story 4.1, 4.2).
  DONE: `recover.py` `decide_recovery(trouble, attempt, replanned)`
  (pure policy) + `honest_report(trouble, plan)`. Wired a bounded
  recovery loop into `run_orchestrated_turn`: nudge re-runs the
  executor with the `recover`-step prompt on the existing transcript
  (carries repair guidance); replan restarts on a clean context with
  only goal + progress-bearing plan re-injected (flailing transcript
  discarded); honest stop delivers a truthful report (status ok, no
  fabrication). Capped at `MAX_RECOVERY_ATTEMPTS=2`, replan at most
  once. Always same alias — no cloud escalation (property C6 / Story
  4.3). Token/iteration totals accumulate across every re-run.
  `test_recover.py` (pure ladder + report) + orchestrator integration
  tests (clean turn = no recovery, nudge recovers empty-after-tools,
  replan uses clean context, honest stop on persistent trouble). Full
  suite 1574 passed.
- [x] 14b. Planner-level continue-nudge for thinking models
  (follow-on from live validation 2026-06-14; see
  `docs/observed-issues.md` "Thinking-model planner stalls"). A
  thinking model (qwen3:14b) emits its plan as `reasoning_content`
  with empty `content` and no `todowrite`; `run_agent_loop` reads the
  no-tool-call turn as a natural stop, so the plan never lands and the
  executor runs plan-less. DONE: `run_planner_pass` detects the stall
  (empty content, no tool call, but output produced — facts only, C4)
  and re-prompts once, feeding the model its own `reasoning_content`
  back and asking it to emit `todowrite`. Gated by `nudge_on_stall`
  (default on); won't fire on a genuine elect-out (non-empty content).
  **Validated live on EC2 qwen3:** stall -> nudge -> qwen3 emits a
  3-step plan -> executor produces a substantive answer (vs shallow
  relay / narrated-JSON in the plan-less runs). `planner_iterations: 2`
  does NOT fix it (confirmed; second iteration never runs past a
  no-tool-call turn). 4 new tests in `test_planner.py`.
  **CAVEAT (walked back 2026-06-15):** "validated live" was n=1 on
  qwen3. Testing gemma4:12b-it-qat showed the nudge is a **narrow
  mitigation for one failure mode**, not a general fix — gemma4 mostly
  plans fine, and its planner failures are different (it calls an
  executor tool from the planner pass, a side effect of the
  executor-tools hint, which the nudge correctly does not fire on).
  Per-model planner-failure characterisation is task-24 audit work.
  See `observed-issues.md` "Thinking-model planner stalls" Update
  2026-06-15.
- [x] 15. Capability-gap ("I'd need a tool to X") is a terminal
  honest outcome, distinct from thrash; not retried/escalated (Story
  4.4). No path rebinds to a cloud alias (Story 4.3; property C6).
  DONE: the recovery loop checks `capabilities.parse_gap(reply)` first
  each iteration — a gap reply is delivered as-is and never
  nudged/replanned over, even when a trouble signal (e.g. a preceding
  tool error) co-occurs. C6 holds structurally: every recovery re-run
  passes `alias=alias` (the turn's own alias), never the planner_alias
  or any cloud alias. Tests: gap terminal with/without a co-occurring
  tool error (no recovery re-run fires).
- [ ] 16. Tests: each trigger classified on synthetic + recorded
  transcripts; recovery never fires on a clean all-success transcript
  (C4 negative); a skip-then-stumble turn self-corrects via re-plan.
  PARTIAL: synthetic per-trigger classification (`test_trouble.py`),
  C4-negative hypothesis property (`test_trouble.py`), and
  skip-then-stumble -> replan self-correction
  (`test_recovery_replan_uses_clean_context` in `test_orchestrator.py`)
  are all DONE. The **recorded-transcript** leg's harness now exists
  (task 3 — `record_replay.py`); capturing a real flailing transcript
  to a cassette still needs a real-model run (12f), so closeout stays
  deferred until then.

## Phase 12d — Visibility

- [x] 17. Emit plan-created, step-start/complete, and re-plan events
  to the Phase 7 turn-event stream (Story 6.1, 6.2). DONE: 4 new
  `TURN_EVENT_KINDS` (`plan_created`, `plan_step_started`,
  `plan_step_completed`, `replan`) + emission helpers in
  `turn_events.py`; the orchestrator emits `plan_created` after the
  planner pass, diffs plan-item statuses after each executor/recovery
  pass into step-started/completed events, and emits `replan` on each
  clean-context restart. None-safe via `tool_ctx.turns`/`turn_id`.
- [x] 18. Render the plan + step progress in the existing live-turn
  renderer and the dashboard turn-detail page; no new viewer (Story
  6.3). DONE: Telegram `turn_renderer` shows a plan checklist
  (✅/🔄/⬜/🚫) at the top of the stream bubble, updated on step events,
  with a re-plan marker; the dashboard turn-detail reconstructs the
  plan + final step statuses from the turn-event stream (no new store)
  and renders a Plan card. Tests: renderer (4) + turn_events helpers
  (5) + dashboard reconstruction via existing detail tests. Plain
  chat / flat-loop turns emit no plan events so their UX is unchanged.

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
  size it degrades), plans-when-nudged, **orchestration-readiness**
  (plans AND follows through on multi-step execution, not just chats —
  the dimension that gates the per-alias `orchestrate` flag),
  context tolerance — across the model roster. Per-dimension, **not**
  a single scalar tier. The profile **informs/surfaces** recommended config to the operator; it
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
