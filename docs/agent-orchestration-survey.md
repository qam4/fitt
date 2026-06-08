# FITT — Agent Orchestration Survey

A source-grounded audit of how three reference agent systems make
imperfect models complete open-ended, multi-step tasks — and what
of it is portable to FITT.

**Why this doc exists.** FITT's premise (project-overview) is a
personal assistant that handles requests of *unknown* complexity on
*deliberately weak / free* models. A flat ReAct loop (today's
`run_agent_loop`, capped at 10 iterations, one model, no recovery)
fails open-ended turns: it either completes or exhausts, with no
move in between. The 2026-06-08 cron-reminder failure was the
symptom that started this. We kept re-deriving the design in chat
and never writing it down; this is the durable capture so the next
"what did we learn?" finds it in one place.

This is a survey, not a spec. When a FITT phase starts, the
relevant section here graduates into
`.kiro/specs/phase<N>-<name>/`.

**Method.** Read from source, not memory. All three systems are
checked out under `.scratch/` (gitignored):

- `.scratch/hermes-agent/` — Hermes (personal-assistant shape)
- `.scratch/openclaw/` — OpenClaw (personal-assistant shape)
- `.scratch/opencode-src/` — OpenCode (coding agent)

File paths below are relative to those checkouts.

---

## The problem, stated once

An open-ended request decomposes into an unknown number of steps.
A weak model run as a flat loop fails three ways:

1. **It can't recover from its own errors** — repeats a broken tool
   call, or narrates instead of calling (the cron failure).
2. **It loses the thread as context grows** — the documented
   granite "narration at ~5K-token prompts / lost in the middle"
   effect (`docs/observed-issues.md`). Discipline degrades with
   context size well before the window's ceiling.
3. **It isn't smart enough for a given step** in one shot.

The single unifying diagnosis across all three reference systems:
**the enemy is context degradation.** Everything below is a cure
for it.

---

## What the three systems actually do

### Hermes — decompose into isolated subagents

Two mechanisms:

- **In-turn subagent delegation** (`tools/delegate_tool.py`). The
  main agent spawns child agents. Each child gets *"a fresh
  conversation (no parent history), its own task_id, a restricted
  toolset, a focused system prompt."* The parent blocks and *"only
  sees the delegation call and the summary result, never the
  child's intermediate tool calls or reasoning."* Children may not
  recurse (`delegate_task` is in the blocked set).
- **Async board decomposition** (`hermes_cli/kanban_decompose.py`).
  A dedicated **auxiliary LLM** fans a "triage" task into a *DAG* of
  child tasks (parent/dependency edges, parallel where independent),
  routes each to a specialist profile, and a dispatcher works them.
  The decomposer itself decides `fanout: true/false` — i.e. it
  classifies "one task or many" upfront. The root task wakes when
  children finish so the orchestrator can judge completion and
  **add more tasks if the work isn't done** (replanning). Each
  child body is written to be read *"by a fresh worker with no
  other context."*
- **Iteration budgets** are generous and per-agent: 90 parent / 50
  per subagent, independent (`agent/iteration_budget.py`).

### OpenClaw — persisted task registry + subagents

A first-class task registry (`src/tasks/`): `queued → running →
succeeded/cancelled`, with **subagent as a runtime kind**
(`childSessionKey`, parent/child flows, `parentFlowId`, queued
retries, `killSubagentRunAdmin`), forked context
(`contextMode: "fork"`), and **detached delivery** of terminal
results back to the user. It has a ground-truth stall signal
(`"...subagent stopped reporting progress"`). `buildToolPlan`
(`src/tools/planner.ts`) is *not* an LLM planner — it's static
tool-visibility assembly; the real orchestration is the task/
subagent system.

### OpenCode — single agent, aggressively managed context

The opposite bet. The core coding loop has **no subagent/task
tool** in its default set (bash, edit, glob, grep, read, skill,
todowrite, webfetch, websearch, write, apply-patch). It keeps one
context and manages it with two mechanisms:

- **`todowrite` tool** (`tool/todowrite.ts`). The plan is a *tool
  the same model calls* to maintain a structured task list as
  durable session state: *"Create and maintain a structured task
  list for the current coding session... track progress during
  multi-step work and keep todo statuses current."* No separate
  planner model. The model writes its own plan and ticks items off;
  the list is re-injected so it stays oriented. The fast path is
  simply not calling the tool.
- **Compaction** (`session/compaction.ts`). When `{system +
  messages + tools}` token estimate exceeds `context − max(output,
  buffer)` (defaults: 20K buffer, keep 8K recent verbatim), a cheap
  `tools: []` summarizer call collapses the old conversation head
  into a structured anchor and keeps the recent tail. The summary
  template is itself a plan-state: **Goal / Constraints / Progress
  (Done/In Progress/Blocked) / Key Decisions / Next Steps /
  Critical Context / Relevant Files.** It preserves exact paths,
  commands, error strings; never mentions that compaction happened.

---

## The unifying insight: two families of cure

Context degradation has two structural fixes, and the three systems
pick different points:

- **Shrink / structure ONE context** — OpenCode: `todowrite`
  (externalize the plan so working memory doesn't have to hold it)
  + compaction (summarize the old head into a structured anchor).
  Good when the task is a single coherent thread over a shared
  workspace.
- **Split into MANY small contexts** — Hermes / OpenClaw:
  decompose into isolated subagents, each with a fresh, small,
  focused context and the parent seeing only summaries. Good for
  independent / parallel work.

These are complementary, not competing. A mature system uses both.

Cross-cutting facts true of all three:

- **None escalate to a smarter model to recover capability.**
  Hermes's failover (`agent/error_classifier.py`) is for rate-limit
  / billing → rotate credential, never "this model is too dumb, use
  a bigger one." They make the chosen model succeed *by structure*.
  This validates FITT's premise: orchestration, not escalation, is
  the lever. (Escalating to cloud would hide the free-model ceiling
  we're trying to find.)
- **The plan is durable state, not working memory.** Whether a todo
  list (OpenCode) or a task graph in sqlite (Hermes/OpenClaw), the
  plan lives outside the model's context and is re-injected.
- **Generous iteration budgets** (Hermes 90/50) vs FITT's flat 10.
- **Context isolation per unit of work** is the shared reliability
  mechanism, stated almost identically in Hermes and OpenClaw.

---

## Don't conflate: planning vs endurance vs decomposition

The single most load-bearing distinction, because mixing these up
leads to building the wrong thing (e.g. expecting compaction to
make a model "plan better" — it can't):

- **Planning** = deciding *what steps to take and in what order*.
  In OpenCode this is `todowrite` (the model writes and maintains
  its own checklist as durable state) plus the model's step-by-step
  reasoning in the loop. The plan lives *outside* working memory and
  is re-injected.
- **Endurance** = keeping the plan and progress coherent as the
  transcript grows. This is **compaction**. It is a janitor, not a
  strategist. On context overflow it drops the noise (verbose tool
  outputs, dead ends) and distills the rest into a tight structured
  anchor (Goal / Done / In Progress / Blocked / Next Steps), keeping
  the recent tail verbatim. The template *looks* plan-shaped only
  because, when summarizing a working session, the goal/progress/
  next-steps are the things most worth preserving. Compaction
  **protects** a plan; it never **produces** one.
- **Decomposition** = splitting a genuinely large task into
  independent (often parallel) sub-tasks with isolated contexts.
  This is **subagents** (Hermes/OpenClaw), and *none* of planning,
  the loop, or compaction does it.

Why it matters for weak models: a complex task is many steps → a
growing transcript → degradation exactly when the task is hardest
(the granite "lost in the middle" effect). The one-context recipe
that sustains it is **`todowrite` + loop + compaction together**:
the model writes a checklist, works it, and when context overflows
the checklist/goal/next-steps survive in the anchor instead of being
truncated away. That buys *endurance*, not *intelligence*. When the
task needs real decomposition into independent pieces, that's a
different mechanism (subagents), added later.

Compaction *alone*, with no `todowrite`, just summarizes a flat
conversation — useful, but the model still re-derives its plan from
prose each time. That's why FITT's first orchestration increment is
the pair, not compaction by itself.

---

## What's portable to FITT (mapped to existing phases)

Ordered cheapest-first. The striking thing: the cheapest cures are
already on FITT's roadmap or fit its existing substrate.

1. **Compaction = FITT Phase 8 (already planned).**
   `session/compaction.ts` is a near-drop-in reference: trigger on
   `{system+messages+tools} > context − buffer`, summarize the head
   into a structured Goal/Progress/Next anchor, keep recent
   verbatim. Cheap, model-agnostic (one summarizer call), fits
   Principle 7. This directly serves weak-model reliability (fights
   the granite degradation), not just window limits.

2. **`todowrite`-style self-plan tool — new, small, high-value.**
   A tool the model calls to maintain a visible task list. Cheaper
   than a separate planner model; model-agnostic; and it plugs
   straight into FITT's Phase 7 visibility work — the todo list
   updates would render live on Telegram, so the user *sees* the
   plan. "How do you know to plan?" becomes "the model opted in by
   calling todowrite"; the fast path is not calling it; a
   ground-truth stall signal is "no todo ticked off in N
   iterations."

3. **Raise / make-configurable the iteration budget.** Flat 10 is
   tight for genuinely multi-step turns. At minimum make it config,
   per Principle 7.

4. **Subagents / decomposition — heavier, later.** Hermes-style
   isolated subagents (fresh context, restricted toolset, parent
   sees only the summary) for genuinely parallel/independent work.
   FITT already has most of the substrate: `run_agent_loop` is a
   reusable headless primitive (drives chat + cron today — a
   subagent is a third caller), the router owns model selection, and
   cron + events + `send_message`/detached-delivery is already an
   async task board with the serial numbers filed off. The
   "monitor X, ping me when done" shape *is* OpenClaw's
   detached-task delivery.

5. **Eval as the proof harness.** Failed live turns become eval
   cases; the eval suite (real tools, multi-step, arg-grading)
   measures whether a given orchestration change crosses a ceiling
   the flat loop couldn't. Production failure → eval case → measure
   → ship. This is what makes the investment compound, and it's the
   real reason to extend the eval beyond synthetic tools.

---

## Recommended shape for the FITT orchestration phase

Synthesis of the above, faithful to FITT's principles:

- **Progressive, not predictive.** Live chat starts on the cheap
  flat loop (fast path, free). Promote to planning/compaction on
  **ground-truth** trouble (repeated tool errors, identical retried
  call, N iterations with zero successful calls, context overflow) —
  never on inferred intent (the `claim_check` / narration-shape
  rollbacks are the scar tissue here; decide on facts).
- **On promotion, restart with clean context**, don't continue a
  flailing transcript — the transcript is itself context poison for
  a weak model.
- **Recovery = decompose / repair / retry / honest ceiling**, never
  escalate-to-smarter-model. When the free model genuinely can't,
  surface it honestly and log it as an eval candidate.
- **Start with the one-context cures** (compaction = Phase 8,
  todowrite self-plan) before the many-context cures (subagents),
  because they're cheaper, visible, and model-agnostic.

---

## Open design decisions (need an explicit call before the spec)

1. **Fail-honest confirmed?** Evidence says no system escalates for
   capability. Confirm FITT's recovery ladder ends at "honest
   ceiling + eval candidate," with at most a bigger-*local*-model
   rung (qwen3:14b) and never cloud-as-crutch.
2. **First increment:** todowrite + compaction (one-context,
   cheap, visible) vs jumping to subagents (many-context, heavier).
   Recommendation: the former.
3. **Phase number / name.** A fresh phase, or reshape the vacated
   Phase 6 (the dormant "Spec-Runner: Unattended Coding")? This is
   general-assistant orchestration, distinct from that coding
   spec-runner, so probably a fresh phase that *depends on* Phase 8
   compaction.

---

## Sources

- Hermes: `.scratch/hermes-agent/tools/delegate_tool.py`,
  `agent/iteration_budget.py`, `hermes_cli/kanban_decompose.py`,
  `agent/error_classifier.py`.
- OpenClaw: `.scratch/openclaw/src/tasks/*`,
  `src/tools/planner.ts`.
- OpenCode: `.scratch/opencode-src/packages/core/src/tool/todowrite.ts`,
  `packages/core/src/session/compaction.ts`,
  `packages/core/src/tool/` (default tool set).

Read from source 2026-06-08. Re-verify against the checkouts before
treating any single detail as current; these are fast-moving
upstreams.
