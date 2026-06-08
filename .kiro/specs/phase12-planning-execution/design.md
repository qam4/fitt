# Design: FITT Phase 12 — Planning & Execution

> Grounded in the source audit of Hermes, OpenClaw, and OpenCode
> (`docs/agent-orchestration-survey.md`). Every load-bearing
> decision cites what a reference system actually does, or is
> flagged explicitly as a FITT simplification/invention. The thesis:
> when frontier models aren't available, the harness is the product.

## Architecture Overview

The orchestrator is a thin control layer *over* the existing
`run_agent_loop` primitive — not a new agent framework.
`run_agent_loop` already drives both chat and cron; the orchestrator
becomes a third caller that runs it in **role-switched passes** and
manages a durable plan between them.

```
                        one user/cron turn
                                │
                                ▼
        ┌───────────────────────────────────────────────┐
        │  Orchestrator  (wraps run_agent_loop)           │
        │                                                 │
        │   (1) PLAN pass                                 │
        │       run_agent_loop with:                      │
        │        - system prompt = resolve(plan, alias)   │
        │        - todowrite affordance offered           │
        │       → writes Plan to PlanStore                │
        │       elected (prompt-nudged)                   │
        │                          │                      │
        │                          ▼                      │
        │   (2) EXECUTE pass(es)                          │
        │       run_agent_loop with:                      │
        │        - system prompt = resolve(exec, alias)   │
        │        - Plan re-injected from PlanStore        │
        │        - compaction (Phase 8) on overflow       │
        │        - ticks todos as steps complete          │
        │                          │                      │
        │             ground-truth trouble?               │
        │              │yes               │no             │
        │              ▼                  ▼               │
        │   (3) RECOVER            final reply / deliver  │
        │       nudge | repair |                          │
        │       re-plan(clean ctx) |                      │
        │       honest stop                               │
        └───────────────────────────────────────────────┘
                                │
                turn events (Phase 7) ── dashboard / Telegram
```

Steps share one execution context in v1 (the "shrink one context"
cure: OpenCode). Subagents (the "split many contexts" cure:
Hermes/OpenClaw) are deferred (Story 3.4).

## Components

### PromptResolver — `resolve(step, alias) -> system prompt`
Resolves the system prompt along two axes (Story 2.5). Steps:
`plan`, `execute`, `compact`, `recover`. Each has a model-agnostic
default; any `(step, alias)` pair may carry an override in config.
This generalizes FITT's *current* per-model capability injection
(which is the degenerate `step=execute` corner) rather than
introducing a new prompt system.
- **Source:** OpenCode ships per-provider execution prompts
  (`anthropic.txt`/`gemini.txt`/`beast.txt`); Hermes a dedicated
  decomposer prompt; OpenCode a dedicated compaction `SUMMARY_TEMPLATE`
  and `todowrite` prompt. Per-step + per-model is what they do.
- **FITT default:** ship per-step defaults; add per-alias overrides
  only where eval (Story 7.5) shows a model needs one.

### PlanStore — durable plan artifact
The plan is persisted state, not working memory, and is re-injected
into the execute pass (Story 1.2). v1 representation: a markdown
todo list (ordered, status per item).
- **Source:** OpenCode `SessionTodo` + `todowrite`; Hermes
  `_hydrate_todo_store` recovers the plan from history because the
  gateway builds a fresh agent per message — FITT has the same
  fresh-per-turn shape (chat + cron), so the same hydrate-from-store
  pattern applies.
- **Property:** structured round-trip, never a lossy prose summary —
  this is the explicit fix for the 2026-05-10 `_persisted_args`
  poisoning (`docs/observed-issues.md`).

### Planner pass
`run_agent_loop` run with the `plan`-step prompt and the `todowrite`
tool offered. In **elected** mode (default) the prompt strongly
nudges the model to emit a plan for multi-step work and the model
self-elects. There is no forced/always-plan mode — under-election is
caught by Recovery (see Overview and Divergences).
- **Source:** OpenCode's elected+nudge ("Use TodoWrite VERY
  frequently… if required"). Forcing (plan-and-execute style) is
  a known fallback but is **not built** in v1 (see Divergences).

### Executor pass
`run_agent_loop` with the `execute`-step prompt and the Plan
re-injected. Marks todos complete as it goes. Compaction fires on
context overflow.
- **Source:** OpenCode keeps one context and manages it with
  compaction; the executor prompt is the existing FITT agent prompt.

### Recovery
Triggered by **ground-truth** signals only (Story 5): a tool
returned an error, the identical tool call was just retried, an empty
response followed tool calls, N iterations with zero successful tool
calls, or budget exhausted. Actions, escalating: inject a continue
nudge → repair a malformed tool call → re-plan on a **clean context**
→ honest stop.
- **Source:** Hermes's empty-after-tools nudge ("process the tool
  results above and continue") is exactly the cheapest rung, and it
  is keyed on an observable fact, for weak models specifically.
- **Scar tissue:** triggers are facts, never prose-shape/intent
  inference — the `claim_check`/narration rollbacks
  (`docs/observed-issues.md`) are why.

### Compaction adapter (Phase 8 dependency)
On overflow, summarize the old transcript into the structured anchor
(Goal/Progress/Next/…), keep recent verbatim. The Plan is in
PlanStore, so compaction can always re-inject it from the store even
if the transcript copy is summarized away — the plan can never be
"compacted out."
- **Source:** OpenCode `session/compaction.ts` (near-drop-in).

### Orchestrator
Sequences plan → execute → (recover) for one turn, owns the
PlanStore lifecycle, emits turn events, and is the single entry both
the chat handler and the cron runner call (as they call
`run_agent_loop` today). It does not contain model intelligence —
it is a state machine driving model passes. The intelligence lives
in the passes (planner/executor models); the control is rules
(Story 5).

## Config surface (models are configuration, not architecture)

Per alias:
- `prompts.<step>: <override id>` for any of `plan|execute|compact|recover` (optional; defaults used when unset).
- `iteration_budget` (replaces today's hard-coded 10; higher default for planned turns — Story 3.3).
- `planner_alias` (optional; defaults to the turn's alias — Story 2.2).

No code change to add an override or flip a mode. Aliases that do
fine on defaults carry no config.

## Design decisions

- **D1 — One role-switched loop, not a planner subsystem.**
  Planning, execution, and recovery are all `run_agent_loop` passes
  differing only by resolved prompt and offered tools.
  *Rationale:* minimal new machinery; reuses a primitive that
  already drives chat + cron. *Divergence:* Hermes has a separate
  decomposer code path; we unify into the existing loop. *Invention*
  flagged for review.
- **D2 — Plan is a markdown todo list, not a DAG.**
  *Rationale:* OpenCode proves a flat todo list is enough for
  one-context execution; Hermes's dependency DAG only earns its keep
  with parallel subagents, which are deferred. Simplification;
  revisit when subagents land.
- **D3 — Elected planning + strong per-model prompt + ground-truth
  recovery; no `forced` mode.** *Source-verified:* all three
  references elect (prompt + recover); none structurally force. We do
  the same and deliberately omit a forced knob. *Lever order for
  under-planning:* better/per-alias prompt (Story 2.4) → recovery net.
  Forcing is a documented fallback to add only if a model proves
  chronically unable to self-elect (see Divergences) — not built.
- **D4 — Prompt resolution keyed by `(step, alias)`.** Generalizes
  current per-model injection. Build per-step defaults now; per-alias
  overrides only on eval evidence (no pre-built grid).
- **D5 — One context + compaction before subagents.** The cheaper
  cure first; subagents are named successor work.
- **D6 — Long/async work rides the existing cron + events +
  detached-delivery substrate**, not a new task queue. FITT's
  "monitor X, ping me" is already OpenClaw's detached-task shape.
- **D7 — Recovery decides on ground truth, restarts re-plans on a
  clean context.** Flailing transcripts are context poison for weak
  models; carry forward only goal/progress.
- **D8 — No in-turn escalation to a cloud model.** A bigger *local*
  planner alias is the only capability lever, and it is operator
  config. Escalating to cloud would hide the free-model ceiling the
  project exists to surface.

## Simplifications & deliberate divergences from the references

Captured so future-us knows these were chosen, not missed:
1. **Unified loop (D1)** instead of Hermes's separate decomposer +
   delegate subsystems. We get planning by swapping a prompt, not by
   adding a framework.
2. **Todo list, not DAG (D2).** No dependency graph, no parallel
   fan-out in v1.
3. **No subagents in v1 (D5).** OpenCode itself ships its core coding
   loop with no subagent tool; we follow that and lean on
   compaction.
4. **No `forced` planning mode.** All three references elect; we do
   too. Structurally forcing a plan (plan-and-execute style) is a
   known fallback but unproven-necessary here, so it is deliberately
   **not built** — the strong prompt + recovery path stands in for it.
   Add it only if a model proves chronically unable to self-elect even
   with a per-alias planner prompt and recovery.
5. **Room to invent.** This is active research. If a simpler trigger,
   a better recovery signal, or a cheaper plan representation emerges
   in use, it supersedes the above — the requirements pin the
   *properties* (Story 5 ground-truth, Story 1.2 durable plan), not a
   frozen mechanism.

## Correctness properties

- **C1** — Once a Plan exists in PlanStore, every subsequent execute
  pass re-injects it; a produced plan is never silently ignored.
- **C2** — In `elected` mode, a turn that produces no plan and then
  hits a ground-truth trouble signal triggers recovery before the
  iteration budget is silently exhausted.
- **C3** — Compaction never removes the active Plan: the executor can
  always re-inject it from PlanStore.
- **C4** — Every recovery decision references only observable facts
  (tool error / identical-retry / empty-after-tools / zero-progress /
  budget). No decision reads the shape or length of model prose.
- **C5** — The Plan round-trips through persist → re-inject without
  structural loss (no `_persisted_args`-style corruption).
- **C6** — No recovery or planning path rebinds the turn to a cloud
  alias.

## Testing strategy

- **Unit:** `PromptResolver` `(step, alias)` resolution + override
  precedence; `PlanStore` round-trip; recovery-trigger classifier on
  synthetic transcripts (each ground-truth signal); plan election +
  re-injection gating.
- **Property (hypothesis, ≥100 iters):** Plan round-trip stability
  (C5); recovery never fires on a clean, all-success transcript (C4
  negative).
- **Eval (Story 7):** the "daily news summary" multi-step case, run
  flat-loop vs planned on the same alias; per-alias under-plan
  detection; real registered tools, not synthetic.
- **E2E:** a cron firing of the news-summary turn end to end —
  plan → execute (with a compaction triggered mid-run) → deliver via
  `send_message` — asserting the Plan survived compaction (C3) and
  delivery happened.

## Open questions

- **Plan representation evolution.** What concrete signal promotes
  v1's todo list to a DAG? (Likely: the first real need for parallel
  subagents.)
- **Clean context for re-plan.** Does the re-plan pass see a trimmed
  transcript or only the goal + PlanStore? Leaning goal + PlanStore.
- **Planner alias default.** Same alias as execution (cheapest) vs a
  bigger local alias by default. Leaning same-alias-by-default,
  override on eval evidence.
- **Latency on the two-machine setup.** Deferred to measurement;
  speculative-parallel stays a non-first-class option (Non-Goals).
- **Where the planner inference physically runs** when planner_alias
  differs (Hub small model vs Compute) interacts with the single-GPU
  contention noted in Phase 7.6.
