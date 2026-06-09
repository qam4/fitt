# Requirements: FITT Phase 12 — Planning & Execution

> **Provisional phase number.** Depends on Phase 8 (compaction).
> Renumber freely, including reclaiming the vacated Phase 6
> ("Spec-Runner") if you'd rather this be its successor — it is the
> general-assistant orchestration that Phase 6's reshape note
> gestured at.

## Overview

FITT's execution today is a flat ReAct loop (`run_agent_loop`): one
model, called repeatedly, choosing each next tool reactively, capped
at 10 iterations, with two exits — natural stop or
`tool_loop_exhausted`. There is no planning step. The "plan" is
implicit and degenerate: it is the user's raw input, and the model
improvises against it. This fails open-ended, multi-step turns on
the deliberately-weak free models FITT targets (the 2026-06-08
cron-reminder failure was the trigger; see `docs/observed-issues.md`
and `docs/agent-orchestration-survey.md`).

**Lived evidence (operator, daily use):** today turns only succeed
when the operator hand-crafts the request to match FITT's internal
tool shapes — i.e. *the human is doing the planner's job*, translating
intent into the exact tool sequence the model needs. The observed
failures of the bound model (`hermes3:8b`) have been *harness*
failures — schema fumble-traps, narration under large prompts — not a
capability ceiling, which supports the working hypothesis that a
competent small model is under-harnessed rather than incapable. This
phase moves that translation from the human into a planner pass.
Whether it succeeds for a given model is exactly what Story 7 measures
(flat-loop fail vs planned success, same model).

This phase gives FITT an explicit **plan** — an artifact a model
produces and then executes against — and the machinery to execute it
without the weak model losing the thread. The design is grounded in a
source audit of Hermes, OpenClaw, and OpenCode
(`docs/agent-orchestration-survey.md`): all three do the same two
things — plan, then orchestrate execution — differing only in plan
representation, execution topology (one context vs many), and which
model plans.

**Premise (non-negotiable for this phase):** the lever is
orchestration, not model escalation. We make a weak, free model
competent by structuring the work, never by routing hard turns to a
frontier model. Escalating to cloud would hide the free-model ceiling
this project exists to find.

Planning is **elected**: the system prompt nudges the model to write
a plan for multi-step work (as OpenCode and Hermes both do, purely
through prompt instruction — "Use the TodoWrite tool... VERY
frequently... if required"), and the model self-elects in-band. The
Story 5 ground-truth recovery net catches under-election. We
deliberately do **not** add a `forced`/always-plan config knob: it
would be speculative machinery for a problem the prompt + recovery
path already handles, and *none of our three references found
structural forcing necessary* — they all elect. Structurally forcing
a plan is the classic *plan-and-execute* style; if some model ever
proves chronically unable to self-elect even with a stronger
per-alias prompt (Story 2.4) and recovery, forcing is a known
fallback to add *then*, not now. The "you can't know beforehand"
constraint rules out an external *upfront* hard/easy classifier; it
does not rule out election, because the model deciding in-band inside
its first inference is not prediction. See
`docs/agent-orchestration-survey.md` ("How the planning trigger
works") for the source.

## Definitions

- **Plan:** an ordered list of steps a model emits for a turn. A
  trivial turn yields a one-step plan.
- **Planner:** the inference (own prompt, optionally own alias) whose
  output is a plan.
- **Executor:** the inference(s) that carry out plan steps.
- **One-context execution:** steps run in a single agent context,
  kept coherent by compaction (Phase 8). The v1 execution model.
- **Subagent execution:** steps run in isolated child contexts. Out
  of scope this phase; named successor work.

## User Stories

### 1. An explicit plan, not input-as-plan

As a FITT user, I want FITT to work out the steps a request needs
before doing it, so that a multi-step request ("summarise today's
news and send it to me") is handled as a sequence of manageable steps
rather than improvised one reaction at a time.

#### Acceptance Criteria
- 1.1 A turn can produce an explicit plan artifact before execution.
  Planning is *elected*: the system prompt nudges the model to write a
  plan for multi-step work and the model self-elects (1.3). There is
  no `forced`/always-plan flag — under-election is handled by recovery
  (Story 5), not a config knob (see Overview).
- 1.2 The plan is persisted as durable state (not held only in the
  model's working context) and re-injected so execution stays
  oriented.
- 1.3 There is no external upfront "is this hard?" classifier. The
  decision to plan is a *prompt* matter — the system prompt instructs
  the model to answer directly when it can and to write a plan first
  when the request needs steps — so the model self-elects in-band.
  This is made safe by the Story 5 recovery net: a turn that skips
  planning and then hits ground-truth trouble is re-planned on a clean
  context, so a bad self-election self-corrects.
- 1.4 The plan representation is documented and stable enough that the
  renderer (Story 6) and eval (Story 7) can read it. A markdown todo
  list is the starting representation; a DAG is a later refinement,
  not required here.

### 2. Planner and executor are distinct roles

As the operator, I want planning and execution to use separate prompts
(and optionally separate model aliases), so that I can make planning
more reliable without touching execution, honouring "models are
configuration, not architecture."

#### Acceptance Criteria
- 2.1 The planner uses a dedicated system prompt whose sole job is to
  emit a plan; the executor uses the existing execution prompt.
- 2.2 The planner alias is configurable and MAY differ from the
  executor alias; both default to the turn's alias when unset.
- 2.3 Selecting a different planner alias requires config only, no code
  change.
- 2.4 The planner prompt MAY vary per alias/model. OpenCode ships
  per-provider planning prompts (`anthropic.txt`, `gemini.txt`,
  `beast.txt`); FITT already injects per-model capability guidance, so
  a per-alias planner-prompt override fits the existing substrate. This
  (a stronger/clearer prompt) is the primary lever when an alias
  under-plans, alongside the recovery net (Story 5).
- 2.5 Prompts are resolved along two axes — `(step, alias)`. The steps
  (planning, execution, compaction summary, recovery nudge) are each a
  distinct prompt role with a model-agnostic default; any step's prompt
  MAY be overridden per alias. This generalizes today's single-step,
  lightly-per-model system prompt to the full matrix. Source: Hermes
  ships a dedicated decomposer prompt and a recovery nudge; OpenCode
  ships a dedicated compaction summary template, a todo-tool prompt,
  and per-provider execution prompts. Build per-step prompts from the
  start (intrinsic); add per-alias overrides only where eval shows a
  model needs one (do not pre-build a full grid).

### 3. One-context execution with compaction

As a FITT user, I want a long multi-step turn to stay coherent instead
of degrading as its transcript grows, so that the result at the end is
as good as the model could give at the start.

#### Acceptance Criteria
- 3.1 Plan steps execute in a single agent context (the v1 topology).
- 3.2 When the context exceeds the compaction threshold, compaction
  (Phase 8) distills the old transcript into a structured anchor that
  preserves the plan, progress, and key facts while dropping raw
  tool-output noise; recent messages are kept verbatim.
- 3.3 The iteration budget is configurable (today's flat 10 becomes
  config, with a higher default for planned turns).
- 3.4 Subagent / many-context execution is explicitly OUT of scope and
  named as successor work.

### 4. Recovery is fail-honest

As a FITT user, I want FITT to tell me honestly when it cannot complete
a step rather than fabricate a result or silently loop, so that I can
trust what it reports.

#### Acceptance Criteria
- 4.1 Recovery actions are limited to: re-plan, repair a malformed tool
  call, and bounded retry of a step.
- 4.2 When a step genuinely cannot complete, the turn ends with an
  honest report naming what failed and at which step — never a
  fabricated result.
- 4.3 No recovery path escalates the turn to a smarter/cloud model to
  paper over capability. A bigger *local* planner alias (2.2) is the
  only permitted capability lever, and it is operator config, not an
  automatic in-turn escalation.
- 4.4 A genuine capability gap ("I'd need a tool to X") is a terminal
  honest outcome, distinct from a thrash/failure, and is not retried or
  escalated.

### 5. Recovery decisions use ground truth, not inferred intent

As the operator, I want the orchestrator's control decisions based on
observable facts, so that we don't repeat the `claim_check` /
narration-shape false-positive rollbacks (`docs/observed-issues.md`).

#### Acceptance Criteria
- 5.1 Triggers for re-plan / retry / give-up are observable facts: a
  tool returned an error, the identical tool call was just retried, N
  iterations with zero successful tool calls, or budget exhausted.
- 5.2 No control decision infers user intent or pattern-matches the
  shape/length of the model's prose reply.
- 5.3 When a turn is re-planned after trouble, execution restarts from a
  clean context — the flailing transcript is discarded, only the
  goal/progress carried forward.

### 6. The plan and its execution are visible

As a FITT user on Telegram, I want to see the plan and watch steps
complete, so that a multi-minute turn is legible rather than a silent
wait.

#### Acceptance Criteria
- 6.1 The plan is surfaced through the Phase 7 turn-event stream and
  rendered by the existing live-turn renderer.
- 6.2 Step start/completion and re-plan/compaction events appear in the
  per-turn event stream and the dashboard turn-detail page.
- 6.3 No new bespoke viewer is built; this reuses the Phase 7 / 4.8
  visibility substrate.

### 7. Planned turns are eval-measurable

As the operator, I want to measure whether planning actually makes a
weak model more capable, so that decisions about planner model/prompt
are driven by data, not vibes.

#### Acceptance Criteria
- 7.1 The eval harness can run a multi-step case end-to-end (plan +
  execute) against an alias and report whether it succeeded.
- 7.2 The eval exercises the real registered tools, not synthetic
  stand-ins (closes the gap from `docs/observed-issues.md`).
- 7.3 A case can compare flat-loop vs planned execution on the same
  alias so the planning delta is measurable.
- 7.4 The "daily news summary" turn is included as a motivating
  multi-step eval case.
- 7.5 The eval reports whether an alias *under-plans* in elected mode
  (skips planning on a turn that needed it), so the per-alias planner
  prompt (Story 2.4) can be tuned from data rather than guesswork.

### 8. Behavior keys off capability, not model identity

As the operator, I want harness behavior driven by what a model can
demonstrably do, not by its name or family, so that swapping or
adding a model is a data operation and FITT never hardcodes
assumptions about a named model.

#### Acceptance Criteria
- 8.1 No harness branch keys on a model name/family substring.
  Per-`(step, alias)` prompt selection (Story 2) and any behavioral
  switch key off a capability profile, not identity.
- 8.2 The profile is a **hybrid**: declared metadata (context window,
  nominal tool support — cheap, static, models.dev-style catalog)
  plus **measured** grades (Story 7.5 / tasks 24) for the
  load-conditioned behaviors a catalog cannot capture (e.g.
  tool-calling reliability at large prompt sizes — the granite case,
  where declared "supports tools: yes" was wrong in practice).
- 8.3 A model whose profile shows it cannot meet a capability an alias
  requires (e.g. tool-calling at the operator's prompt size) is
  flagged unsuitable at bind/boot — fail-loud (Principle 11), not
  silently bound.
- 8.4 Reference contrast (rationale, see
  `docs/agent-orchestration-survey.md`): Hermes keys behavior on
  model-name/family substring matching (including whether a model
  "supports tool calling"); OpenCode uses a declared capability
  catalog (models.dev); neither runtime-measures. FITT's
  single-operator, few-models, stress-weak-models context justifies
  the measured layer they omit.

## Non-Goals (this phase)

- **Subagent / many-context execution.** Named successor work; v1 is
  one-context + compaction.
- **Speculative parallel execution** (run direct + planner concurrently
  to hide planner latency). Latency optimisation, explicitly not
  first-class; revisit only if measured easy-turn latency on the real
  two-machine setup proves painful, and only with side-effecting tools
  gated until the planner verdict.
- **Cloud escalation for capability.** Ruled out by the premise.
- **DAG plan representation.** Markdown todo list first; DAG later.

## Prerequisites

- **Phase 8 (compaction)** — Story 3.2 depends on it. Build or land
  compaction first; this phase consumes it.
- **Phase 7 (visibility)** — Story 6 reuses the turn-event stream and
  renderer (already shipped).
- Eval harness extension to real registered tools (7.2) may be done
  here or as a small precursor.
