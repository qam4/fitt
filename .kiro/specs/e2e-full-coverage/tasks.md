# Implementation Plan: Full e2e Coverage

**Status:** in progress — started 2026-08-12.

Build the deterministic layer first (it needs no model, no tunnel, and
gates CI), then the coverage report that makes the gap visible, then the
judged scenarios for user-facing behaviour, then the untested surfaces.

Status legend: `[x]` done, `[ ]` not yet.

## Phase A — Tool-contract layer (no model, no tunnel)

- [x] 1. `gateway/src/gateway/tool_contracts.py`: `ContractCheck`
  (tool name, valid-args factory, side-effect assertion, invalid-args
  case) + `run_contract_checks(registry, ctx)` returning per-tool
  results. Pure over an injected registry + context. (D2; Property 3)
  DONE 2026-08-12. Also carries `skip_reason` (fixture unavailable) and
  `known_broken` (understood, unfixed defect: reported, doesn't fail the
  suite, and flagged loudly if it starts passing).
- [x] 2. Tests with a fake registry: an ok-tool passes, a tool that
  raises on bad args is reported failing, a tool returning `err` for bad
  args passes. DONE 2026-08-12: 11 tests.
- [x] 3. Fixtures: temp FITT home, registered temp project, temp git
  repo, file tree — DONE via `tool_contract_suite.build_project` /
  `init_git_repo` reusing `_fixtures.build_test_config`, plus
  `stub_http_server` (a local one-route server) so `http_get` is checked
  without touching the network.
- [x] 4. Read-mostly surface: `read_file`, `list_directory`,
  `grep_repo`, `list_capabilities` pass; `glob_search` marked
  known-broken (real Windows defect found by this layer — see task 24).
  `http_get` deferred with task 3's stub server.
- [x] 5. State tools: lessons (`learn_add` with a side-effect assertion
  on the store, `learn_list`, `learn_remove`), the full todo lifecycle
  (`todo_add`/`todo_list`/`todo_done`/`todo_remove`) and the full cron
  lifecycle (`cron_add`/`cron_list`/`cron_update`/`cron_pause`/
  `cron_resume`/`cron_remove`). Each works on a row it creates itself, so
  the suite is order-independent.
- [x] 6. Write/code surface — coverage only, no investment:
  `write_file`, `edit_file`, `todowrite`, `project_shell`, `run_tests`,
  `git_commit`, `spec_*`. (`git_status`/`git_diff` done in task 4's pass.)
- [~] 7. `fitt eval contracts` command DONE (exits 1 on a real failure;
  skips honestly when no project is registered). CI wiring still to do.
- [ ] 24. **Fix `glob_search` on Windows** (found by task 4). It shells
  argv `["find", ".", "-type", "f", "-name", p]`, and on a Windows hub
  `find` resolves to Windows `FIND.EXE`, so the model gets "FIND:
  Parameter format not correct" instead of matches or a readable error.
  Options: implement the local path in Python (`Path.rglob`) and keep
  `find` for SSH-backed projects, or fail with a clear
  "needs POSIX find" message. The first is better — it removes a
  platform dependency from a read-side core tool.
- [ ] 25. **Don't cache a transient shell-probe failure.**
  (Supersedes an earlier, wrong version of this task that blamed the
  hardcoded Git Bash path — `bash` is on PATH here and matches candidate
  #1; verified by running it.) `LocalShellProbe.detect` caches
  `ShellInterpreter.none()` for the process lifetime, so one flaky probe
  — Git Bash on this host intermittently fails to fork with cygwin
  `Win32 error 299` / `error 5` — disables `project_shell` on local
  projects until the gateway restarts, with no retry. Caching a success
  forever is right; caching a transient failure forever is not.

## Phase B — Coverage report

- [x] 8. `exercises_tools` on `TaskScenario` (intent, distinct from
  `requires_tools`) and `EXEMPT` map with reasons. (D1; Property 5)
  DONE 2026-08-13: the field shipped with the routing work; now
  backfilled across the whole seed set. Two scenarios declare *no*
  intent on purpose (`memory_recall`, `asks_before_acting`) — any recall
  channel counts for the first, and the second is about *not* acting.
- [x] 9. `fitt eval coverage`: registry minus (scenario intent + contract
  checks + exemptions) -> uncovered names + counts. (R2.1-2.3;
  Property 1) DONE 2026-08-13 — `tool_coverage.py` + the CLI command.
  Offline, exits 1 on an uncovered tool so it can gate. Reports the two
  axes separately rather than as one score (they answer different
  questions), states in the output that the judged column is *intent*,
  and separates "absent by configuration" (`memory_search` on a
  retrieval-off deployment) from "absent by mistake" — a rename, which
  otherwise looks identical because a check for a missing tool is
  skipped on purpose. The conditional list is derived from the
  scenarios' own `requires_tools`, so there's no second copy to drift.
  **Standing: 34 registered, 31 contract-checked, 7 judged, 0
  uncovered** (was "7 of 34 ever exercised").
- [x] 10. Test: registering a tool makes it appear uncovered with no
  other edit. (Property 1) DONE 2026-08-13, plus a test asserting the
  *live* registry is fully covered — so the standing claim is checked
  rather than retyped, and adding a tool without a check fails a test.

## Phase C — Proactive behaviour (judged)

- [x] 11. **No sink needed — D3 was wrong.** `send_message` already
  records delivery by appending an `agent_message` event to the event
  log; the Telegram poller is a separate subscriber to that log. So the
  log *is* the delivery record. `snapshot_app` now captures
  `agent_messages` (title/body/session) instead. DONE 2026-08-12.
- [x] 12. `notify` scenario: "send me a message on my phone reminding
  me...". Objective check reads the delivery record, so a model that
  merely *claims* to have sent fails. DONE 2026-08-12. **Live: gemma4
  PASSES** (send_message:ok, judge 1.00) — proactive push verified for
  the first time.
- [x] 13. `cron_fires` scenario + `TaskScenario.settle`, a new hook that
  runs after the turns and before the snapshot. A cron fires on a
  scheduler tick, not in response to a turn, so the hook forces
  `cron_scheduler.tick(now=+1h)` and awaits the firings — no sleeping.
  Distinguishes never-fired / fired-but-session-failed / fired-but-
  delivered-nothing. DONE 2026-08-12; first live run found a harness
  confound (task 26), re-measuring.
- [ ] 14. Cron cancel/pause scenarios using the setup hook, so
  cancelled is distinguishable from never-created. (R3.3)
- [x] 26. **Pin `fitt-default` to the DUT during an eval run.** Found by
  task 13's first live run: a cron job with no explicit `agent_alias`
  resolves to `fitt-default`, or to the *first alias in the map* when
  that's absent — `fitt-local-qwen3` in the dev config. So the chat turn
  was measured on gemma4 while the model-initiated half of the work went
  to an unreachable local model (`cron_failed: NoBackendAvailable`).
  Any future scenario where FITT starts its own session would have been
  mismeasured the same way. DONE 2026-08-12.

## Phase D — Read-mostly project queries (judged)

- [ ] 15. Scenario: "what's in <project>?" -> `list_directory` /
  `glob_search`. (R1.1)
- [ ] 16. Scenario: "find where X is defined" -> `grep_repo` +
  `read_file`, asserting the reply cites the real path. (R1.1)

## Phase E — Untested surfaces

- [ ] 17. Approval flow: approve / reject / timeout with a scripted
  decider; assert the tool ran or didn't, and that the audit log
  recorded the decision. (D5; R4)
- [x] 18. Skills loader: a temp skill is offered to the model and
  appears in the capability surface. (R4) DONE 2026-08-12 via the
  `skills` scenario + `TaskScenario.fixture_files` (planted *pre-boot*,
  because `SkillsLoader` scans once at startup and deliberately never
  re-reads) + `requires_features` so a skills-off deployment reports
  unsupported instead of a model failure. **Live: gemma4 PASSES** —
  loaded the recipe and applied it.
- [x] 19. Planned-mode orchestration: same scenario via `--mode planned`,
  compared against flat. (R4) DONE 2026-08-14 — see tasks 71-74.
- [ ] 20. Prefetch: with `prefetch_enabled`, recall works with no
  `memory_search` call — and the cross-session assertion must learn
  about this FOURTH recall channel before this lands, or it will report
  a retrieval failure while the answer arrives correctly. (R4; carried
  warning from observed-issues)
- [ ] 21. Telegram command handling (`/model`, `/status`, `/lastturn`) —
  or an explicit exemption if the telegram-bot package's own suite is
  judged sufficient. (R4; R2.3)

## Phase F — Standing view

- [ ] 22. Matrix renders contract results as a separate table, not as
  rows with a meaningless model column. (D6)
- [~] 23. Regenerate `docs/feature-model-standing.md` with the full set
  and re-measure all three DUTs (`--samples` > 1 for anything cited).
  Regenerated 2026-08-13 with all 14 scenarios × all 3 DUTs — no `-`
  cells, so the columns are comparable for the first time (gemma4 14/14,
  qwen3 12/14, hermes3 7/14). Still `samples=1`, which the backlog is
  explicit about: hermes3 has scored 3/7 and 4/7 on consecutive identical
  runs. Every cell is indicative until it's multi-sampled.


## Coverage audit (2026-08-13) — read this before the gap lists

The 2026-08-12 pass below called itself an audit. It was a **listing**:
one bullet per roadmap phase, written from `FITT_ROADMAP.md`'s phase
list. Task 29 read "planned mode has a spec and no e2e coverage" —
technically true, and it hid three concrete facts that one level of
investigation turned up a day later (the driver has no mode concept at
all; every matrix cell is the flat loop and nothing says so; the
routing gate into the orchestrator is untested). If that bullet was
shallow, the rest were, and anything without its own roadmap phase was
never looked at.

So it was redone properly on 2026-08-13, enumerating from the code
rather than the roadmap. **The single structural finding, which explains
why the first pass had to miss what it missed:**

> No measurement layer in the repo can see a cross-cutting subsystem.
> `fitt eval coverage`'s denominator is the tool registry, and the
> contract layer calls `tool.callable(args, ctx)` directly — bypassing
> approval, the deny list, the audit log, the rate limiter, artifact
> hoisting and the agent loop. So it measures *tool implementations*,
> never *the path a tool is reached by*. Auth, cost accounting, fallback
> routing, approval-policy resolution, the HMAC audit chain, rate
> limiting, boot warnings, the startup hooks, the CLI and Open WebUI are
> outside both axes **by construction, not by oversight.**

The 2026-08-12 pass correctly spotted that Phases A-F were
registry-scoped — and then added items that were still mostly *features
with tools or scenarios*. The infrastructure spine stayed unmeasured, and
"0 uncovered" was reported while it was. (`tool_coverage.py`'s render now
states its own scope out loud, and `test_tool_coverage.py` pins that.)

Three more things the audit turned up that are worth stating before the
lists, because they change how the existing numbers should be read:

1. **`samples=1`, and no config is recorded.** `fitt eval e2e` pins five
   things on the loaded config (FITT_HOME, `fitt-default`, the memory
   paths, auto-approve, and `record_llm_requests` at Tier 3) and
   **inherits everything else from the operator's `config.yaml`. Zero
   config values reach the report or the sidecar.** ~15 settings can
   change a verdict — see task 47. Two of them turned out to be live
   defects, now fixed (tasks 48, 49).
2. **Several assertions are weaker than the matrix implies.** `chitchat`
   requires a reply and *zero* tool calls, so it gets *more* likely to
   pass as the tool subsystem breaks. `_routing_assert` returns "noisy
   but right", so a model firing all three of `cron_add`/`todo_add`/
   `send_message` on every request passes all three routing scenarios.
   `asks_before_acting` accepts any reply containing "?". See task 50.
3. **"31 contract-checked" includes ~8 tools that don't run or don't
   work**: four `skip_reason` skips report as `passed=True, skipped=True`;
   `glob_search` and `project_shell` are `known_broken`; and five checks
   (`todo_list`, `cron_list`, `learn_list`, `list_capabilities`,
   `todowrite`) have no side-effect and no invalid-args case, so they
   pass on any function returning a non-error `ToolResult`. See task 51.

Method note for next time: an audit derived from a phase list can only
find phases. Enumerate from the code, then ask of each item "which layer
would catch this breaking, and would it actually fail?"

## Roadmap-derived gaps (added 2026-08-12, kept for continuity)

*(Not a roadmap phase. The lettered phases above are sections of this
spec's plan; FITT's roadmap phases are numbered — 4.5, 9, 12 — and the
items here are cross-references INTO that roadmap, not a new phase of
it.)*

Phases A-F were scoped from the *tool registry*, which is why they miss
whole features: a capability with no tool (skills) or a different
execution mode (planned) is invisible to a registry-derived count. Mapped
against `FITT_ROADMAP.md`, these ship today and the standing view says
nothing about them:

- [~] 27. **Skills loader (Phase 4.10).** Scenario DONE 2026-08-12 (see
  task 18). Still open: a contract-style check that a malformed
  `SKILL.md` is rejected with a readable error rather than breaking boot.
- [ ] 28. **Lessons applied later (Phase 5).** `learn_*` are
  contract-checked, but nothing tests the *point* of lessons: correct the
  assistant, then see the correction honoured on a later turn. Note the
  global-lessons channel already caused two false verdicts here, so this
  scenario must isolate its fixture (see R5.4).
- [ ] 29. **Planned mode (Phase 12).** Orchestration has a full spec,
  shipped code, thorough *fake-driven* unit tests — and no judged
  coverage at all. The judged driver has no mode concept: `e2e_driver`
  POSTs to `/v1/chat/completions`, so the loop is chosen downstream by
  `config.is_orchestrated(alias)`, which is default-off. Every number in
  the standing matrix is therefore the flat loop, and nothing in the
  report says so. Two parts, in order:
  - **Make the mode explicit and visible.** A `--mode flat|planned` flag
    on `fitt eval e2e` that sets `cfg.orchestration` for the DUT, and the
    loop that ran recorded in the sidecar. Today an operator whose real
    config has `orchestration.<dut>.enabled: true` would silently measure
    planned mode and never know.
  - **Get a discriminating scenario first.** Phase 12 task 24 deferred
    "orchestration-readiness" as a profile dimension for exactly this
    reason: `daily_news_summary` doesn't *need* sequencing, so it can't
    show planning's leverage. Comparing flat vs planned on a scenario
    that doesn't reward planning measures nothing.

  Carry the confound forward, or it gets re-derived: the one real-model
  comparison we have (task 22, hermes3:8b, n=5) found planning did **not**
  beat flat — but task 23 then found hermes3 elects to plan **0%** of the
  time, so "planned mode" ran plan-less and the comparison was flat vs
  flat. **Planning has never been measured on gemma4**, the model that
  scores 14/14 and is the recommended binding. "Planning doesn't help" is
  not a finding yet.
- [ ] 46. **Test the routing gate into the orchestrator.** `chat.py` and
  `cron_runner.py` each guard `run_orchestrated_turn` behind
  `config.is_orchestrated(alias)` + a prompt resolver + a plan store. The
  gate is tested (`test_config.py`) and the orchestrator is tested
  (`test_orchestrator.py`, which calls it directly) — the *wire between
  them* is not. So an orchestrated alias silently falling back to the
  flat loop would pass every test we have. Cheap: one test per call site
  asserting the orchestrator is reached.
- [ ] 30. **Telegram command surface (Phase 3 / 7).** `/model`,
  `/status`, `/lastturn`, `/eval`, and the markdown renderer. Decide
  explicitly whether the telegram-bot package's own suite is sufficient —
  if so, record it as an exemption with that reason rather than leaving
  it looking forgotten.
- [ ] 31. **Gateway contract (Phase 1).** No scenario asserts the alias
  discipline that Principle 7 rests on: a concrete model id must be
  rejected with HTTP 400, a bad token with 401. Cheap, deterministic,
  and it guards a rule the whole config model depends on.
- [ ] 32. **Compaction (Phase 8)** and **visibility surfaces (4.8/7)** —
  cover when they matter; compaction isn't exercised until sessions are
  long enough to trigger it.
- [ ] 33. **Fold the alias-eval suites into one standing view.**
  `fitt eval` already runs `default` / `coding` / `realistic` suites with
  their own reports and sidecars — a separate ladder rung from the judged
  e2e harness. Two places to look is one too many: `fitt eval matrix`
  should show both, clearly labelled, so "what works today" is a single
  question with a single answer.

Also note the standing's denominator is per-deployment: `memory_search`
only registers when an embedding alias is bound, so the registry is 33 or
34 tools depending on config. Any coverage percentage has to say which.


## Routing + scenario-premise discipline (added 2026-08-13)

Three scenarios and two guard mechanisms that came out of the
send/cron/todo routing triangle. Grouped here because they share a
lesson: a scenario is only fair if the behaviour it demands is behaviour
FITT actually advertises, and *that* premise needs pinning too.

- [x] 40. **Document the third edge of the routing triangle.**
  `cron_add` and `todo_add` spelled out their boundary; `send_message`
  was described purely as agent-initiated, so the everyday "text me X"
  wasn't advertised anywhere and the model had to infer it. All three
  descriptions now carry the same three-way rule (time -> `cron_add`, no
  time -> `todo_add`, wants it now -> `send_message`, ambiguous -> ask).
  DONE 2026-08-13.
- [x] 41. **Routing scenarios: `routing_timed`, `routing_untimed`,
  `routing_push_now`.** Assert the documented rule actually holds, which
  turns hermes3's observed mis-route (reaching for `todo_add` when a
  timed cron was wanted) into a named failure rather than a footnote.
  Check the side effect, not the tool call, and name what it got instead
  so a miss says where the request went. **Live: gemma4 passes all
  three.** DONE 2026-08-13.
- [x] 42. **Pin the premises (`tests/test_scenario_premises.py`).** Task
  40 silently invalidated `asks_before_acting`: it was built on wording
  that was unresolvable, `send_message`'s description then claimed that
  wording, and the scenario carried on asserting a failure for behaviour
  the prompt now endorses. Nothing failed — an **unpinned premise**, a
  test depending on a property of production text that lived nowhere.
  Now: the triangle must stay closed, the timed/untimed clauses must
  exist verbatim, `routing_push_now` must use a phrasing `send_message`
  advertises, and `asks_before_acting` must use one no description
  claims. Plus a guard on the extraction helper itself (possessives like
  "the user's phone" mis-pair a naive quote regex and turn every check
  vacuous) and one asserting `_ACTING_TOOLS` names real tools.
  DONE 2026-08-13.
- [x] 43. **Report objective↔judge disagreement.**
  `E2EReport.disagreements` + a `render()` line. The two layers fail
  differently — code can only be wrong about the *scenario*, the judge
  only about the *reply* — so a split says one of them is broken, and it
  is at least as often the scenario. It's what exposed task 42's stale
  scenario. Documented limit: the judge is anchored on the objective
  verdict, so it's biased toward agreement; a hit is strong evidence,
  silence is weak (task 34 is what would change that). DONE 2026-08-13.
  **It has since caught three distinct things in three runs:** a stale
  scenario (code wrong), a cross-talk-poisoned judge (judge wrong — task
  45), and a too-lenient objective check (judge right — qwen3 passed
  `news_summary` while fabricating a multi-source summary from one thin
  search result; backlogged).
- [x] 45. **Tell the judge what the internals attribute.** With task 44
  in, the disagreement line fired on the next run: `objective=PASS
  judge=FAIL`, because the judge's snapshot is the *cumulative* end state
  and it made the same inference the assert had just been cured of ("it
  also created a cron job 'Call the doctor'" — another scenario's — plus
  "invented a time (9:00)", which the user had given). The prompt now
  states that the tool list is this turn's and the side-effect state is
  the run's, an entry with no matching tool call is not this turn's
  doing, and drops the "GROUND TRUTH — what actually happened" heading
  over the whole block, which was true of the tools and false of the
  state. Not a substitute for task 34 or for per-scenario state
  isolation. DONE 2026-08-13.
- [x] 44. **Attribute action to the turn, not the end state.** The
  corrected `asks_before_acting` then failed a *correct* model: it asked
  the clarifying question and called nothing, and was blamed for the
  `reminder` scenario's leftover cron. Scenarios with a subject filter
  the snapshot by keyword; this one has no subject, so it must read the
  turn's own `tool_calls`. The general rule now recorded in
  observed-issues: snapshot-only asserts are only safe when they can
  attribute the side effect to the turn. DONE 2026-08-13.

## Planner coverage (added 2026-08-14)

Closes task 19 and the first half of task 29. Ordered as built, because
each step was a precondition for the next: without the mode pin the
scenario is meaningless, and without the session fix the mechanism
assertion always reads "no plan".

- [x] 71. **`--mode flat|planned` on `fitt eval e2e`, pinned and
  recorded.** Sets `orchestration.<alias>.enabled` for the DUT **and**
  `fitt-default`: `is_orchestrated` keys on the *alias name* while the
  command only repointed fitt-default's *model id*, so a config that
  orchestrated one name gave a half-orchestrated run — graded turns on one
  loop, cron firings on the other. Fails loud (exit 2) if `--mode planned`
  is asked for without `prompt_resolver` / `plan_store` on `app.state`,
  rather than silently degrading to flat while the report says "planned"
  (Principle 11). `mode` is written to the sidecar; older sidecars read as
  `unrecorded` rather than being back-filled as `flat`, because they were
  flat in practice but nothing pinned it. DONE 2026-08-14.
- [x] 72. **The matrix splits columns by loop mode.** `latest_per_dut`
  keyed on DUT alone, so a planned run silently overwrote the flat run for
  the same model — destroying the comparison it was run for. Columns are
  now DUT x mode (`gemma4` / `gemma4 [planned]`); `flat` and `unrecorded`
  deliberately share a column so a newer pinned run supersedes an old
  unpinned one, and the render calls out `loop=unrecorded` as
  uninterpretable. DONE 2026-08-14.
- [x] 73. **`multi_step_chain` — a scenario that actually rewards
  sequencing.** Phase 12's close-out deferred orchestration-readiness
  because `daily_news_summary` doesn't need sequencing. This one has a real
  dependency chain: read the todo list, schedule a reminder for the item
  that has a date in it, then summarise what was scheduled. Step 2's
  arguments are only knowable from step 1's output. Todos are planted as a
  **pre-boot fixture**, so the first step is a genuine read of state the
  model didn't author. The assert is keyword-filtered (`passport` /
  `mattress`) per the cross-talk discipline, distinguishes
  stopped-after-step-2 from acted-on-the-wrong-item, and — unlike the
  routing scenarios — treats scheduling both items as a failure, because
  the request said only the dated ones. Runs in both modes, so it *is* the
  flat-vs-planned comparison. DONE 2026-08-14.
- [x] 74. **`planner_elects_a_plan` — the mechanism, split from the
  outcome.** Same request, gated on the `planning` feature so a flat run
  reports *unsupported* instead of failing (the `memory_search` lesson).
  Asserts a plan of >= 2 steps exists **and** that at least one was marked
  complete — a plan the model didn't work is its own failure. Distinguishes
  "elected not to plan", which is the confound that invalidated the Phase
  12 comparison: hermes3 planned 0% of the time, so flat-vs-planned was
  flat-vs-flat. Required `snapshot_app` to gain `plan_items`, and the
  driver to pass the **scenario's** session id — the plan store is keyed by
  session, so the previous default of `"main"` would have made every plan
  look un-elected. DONE 2026-08-14.

### Result (2026-08-14, gemma4:12b-it-qat, judge pinned)

Final, after two scenario-design fixes (tasks 77, 78):

| loop | objective | judge | planner_elects_a_plan |
|---|---|---|---|
| flat | **15/15** | 15/15 | n/a (feature off) |
| planned | **15/15** | 15/15 | inconclusive — elected not to plan |

The first attempt read differently and is kept below because the two
withdrawals are the useful part of the record:

| loop | objective | judge |
|---|---|---|
| flat | 15/15 | 13/15 |
| planned | 15/16 | 15/16 |

The one planned-mode failure was `planner_elects_a_plan`: **gemma4 elected
not to plan.** Trace was `todo_list, todo_list, cron_add, send_message` —
it did the work and never called `todowrite`.

**The conclusion first drawn from that is withdrawn.** I read it as
falsifying Phase 12 task 23's model-weakness explanation and proving the
bottleneck is *elicitation*. The task doesn't support that: it enumerated
its three steps in order and exactly one todo qualified, so a plan would
have restated the prompt with nothing to track. Declining was plausibly
correct judgement, and the assertion calling it a failure was punishing
good behaviour — the third assert this month to do that.

What stands: no demonstrated benefit from planned mode, both historical
comparisons were effectively flat-vs-flat, and gemma4 sequenced an easy
multi-step task flat. What's withdrawn: any claim about *why* models
decline. Two explanations remain live (the prompt doesn't elicit it; the
tasks didn't need it) and task 77 is what separates them.

- [x] 77. **Replace the task with one that warrants a plan, and make
  no-plan `inconclusive`.** `multi_step_chain` retired; `deadline_sweep`
  replaces it — five todos planted pre-boot, three dated, interleaved with
  two undated, and the request states a *goal* ("I keep missing deadlines
  … make sure I get reminded about every one of them in time") with no
  steps and no count. The model must derive both, and completeness across
  three items is a live risk. A test asserts the request stays goal-shaped
  (no "first"/"then", no leaked count) since the failure mode is the
  wording drifting procedural. No plan now returns `inconclusive` rather
  than `FAIL`: a model that succeeds without one hasn't failed, and such a
  run establishes only that it can't measure planning. Retiring rather
  than keeping the old scenario was forced — both plant `todos.md` into
  one shared pre-boot run home, so the last silently wins; a test now
  asserts a single `todos.md` fixture across the seed set.
  DONE 2026-08-14.
- [x] 78. **Fix `deadline_sweep`'s own underspecification.** Its first
  wording failed 0-of-3 on *both* loops, and the trace showed why: gemma4
  identified exactly the right three items, left the undated ones alone,
  proposed firing two days early, and asked before creating three crons.
  The request ("make sure I get reminded … in time") named no lead time, so
  asking about an invented parameter was correct — and is what
  `asks_before_acting` rewards. Two scenarios in one suite with opposite
  incentives. Fixed by supplying the lead time and explicit permission
  ("go ahead … no need to check with me first"), with premise tests pinning
  both, plus one asserting `asks_before_acting` never grants permission.
  **General constraint recorded:** a scenario asserting a multi-item side
  effect must supply every parameter the action needs and pre-authorise it,
  because the harness has no human to confirm with. DONE 2026-08-14.
- [~] 75. **Separate the two explanations for non-election.** Answered for
  **gemma4** only — which is the wrong model to ask. Phase 12's
  requirements target "the deliberately-weak free models FITT targets" and
  Story 7.3 defines success as *flat-loop fail vs planned success, same
  model*; gemma4 passes everything flat, so it cannot show a delta by
  construction. Re-open against hermes3 (7/14 flat). Note also that the
  "reframe" recorded below is a re-derivation of Story 7.3's stated
  criterion, and that `forced` mode — listed earlier as a lever — is ruled
  out by name in the requirements, with reasons.

  With `deadline_sweep` properly specified: **flat 15/15, planned 15/15**,
  judge 15/15 both. gemma4 passed the sweep *on the flat loop* — five
  todos, selection and count derived from the data, three crons created,
  undated items untouched — and still elected not to plan.

  So: not an elicitation problem. For a capable model a
  planning-conducive task is *by definition one the flat loop fails*, and
  this suite has none. I designed for "rewards sequencing" when the
  operational test is "flat can't do it". Three non-elections, all on
  tasks the model could complete flat, is good judgement.

  **Conclusion: on gemma4, orchestration has no demonstrated use case.**
  Leaving it off (the default) is correct for this binding. That is a
  real answer, not a gap.
- [ ] 79. **If planning is revisited, do it on a weaker model or a harder
  task — not on gemma4.** The null result above is specific to a model
  that passes everything flat. hermes3 scores 7/14 flat and is the
  population planning might actually help; a task gemma4's flat loop
  genuinely fails (longer horizon, more items, a real mid-task branch)
  would be the other way in. Until one of those exists there is nothing
  to measure, so the earlier levers (plan-prompt tuning, `planner_alias`,
  the deferred `forced` mode) have no target and shouldn't be built.
- [ ] 76. **Multi-sample before citing any flat-vs-planned delta.** Both
  columns are `samples=1`. Two identical runs 12 minutes apart also
  disagreed on two *judge* verdicts (`reminder`, `asks_before_acting`), so
  the judge columns are no more comparable than the objective ones.

Remaining from task 29: nothing structural. What's left is measurement,
plus task 75 — which is the interesting half, and it's Phase 12 work
rather than harness work.

## Audit findings (added 2026-08-13)

Everything the 2026-08-12 listing missed. Ordered by whether a *wrong
conclusion* is currently reachable, not by size.

### Fixed on discovery

- [x] 48. **The cron runner held the un-wrapped approval middleware.**
  `create_app` passes `app.state.approval` *into* `CronRunner` at
  construction, and the harness swapped `app.state.approval` for the
  auto-approver afterwards — so the runner kept the real one. The
  `cron_fires` scenario runs a real agent session through that runner, and
  a cron with `approval_mode` unset (the default a model creates) would
  hit an ASK-bucket tool, block for the 10-minute `approval_timeout_secs`,
  reject, and be recorded as a model failure. It survived only because the
  tool it happens to call is AUTO-bucket. Fixed by
  `e2e_driver.auto_approve_for_eval(app)`, which owns the hazard in one
  place; three tests, one pinning the construction-capture itself.
  DONE 2026-08-13.
- [x] 49. **Eval logs escaped the isolated run home.**
  `isolate_memory_paths` promised "every FITT_HOME-derived path" and
  checked only `cfg.memory`, so `cfg.logging.dir` — resolved at config
  load, before `FITT_HOME` is redirected — kept pointing at the operator's
  real `~/.fitt/logs`, appending eval runs' logs and, under
  `server.log_bodies`, their full request bodies. Exactly the class the
  function's own docstring warns about, one field family over, invisible
  because the assertion's scope was narrower than the claim. Renamed
  `isolate_run_paths`, now redirects and asserts over `logging` too.
  DONE 2026-08-13.

### Wrong conclusions currently reachable

- [ ] 47. **Pin and record the harness config.** `fitt eval e2e` inherits
  ~15 outcome-changing settings from the operator's config and records
  none of them, so two operators can produce different numbers under the
  same DUT name and no artifact shows why. **Pin** (the harness must
  decide, not the operator): `tools:` buckets + `per_client` (a `block`
  fails whole scenarios as model failures), `orchestration:`/`prompts:`,
  `approval_detach_threshold_secs` (pure wall-clock on the loop — a slow
  turn returns the "⏳ Approval pending…" placeholder *as the graded
  reply*), `loop_brake_enabled`, `memory.enabled`, `web.search_backend`,
  `max_history_chars`, artifact-hoist thresholds, `prefetch_enabled`, the
  send_message limiter. **Record** (DUT identity or a deliberate choice):
  the client tag — and fail loud if the selected token is tagged
  `coding-agent`, which is router-mode and would fail all 14 — the
  fallback chain plus per-turn `fallback_used` (a transport blip silently
  measures the *fallback* model under the DUT's name), `num_ctx` /
  `backend` / `endpoint`, the warm/VRAM facts the harness already computes
  and prints and then throws away, `upstream_timeout_secs` (and derive
  httpx's timeout from it instead of hardcoding 300s), `--judge-detail`,
  and a hash of registered tool names+descriptions. One `harness` object
  in the sidecar covers it; most values are already in hand.
- [ ] 50. **Tighten three assertions that pass for the wrong reasons.**
  `chitchat` demands a reply and zero tool calls, so it gets *more* likely
  to pass as the tool subsystem breaks — it needs a positive signal that
  tools were available and correctly declined. `_routing_assert`'s "noisy
  but right" branch passes a model that fires all three routing tools on
  every request; treat firing the other two as a failure, since routing is
  the thing under test. `asks_before_acting` accepts any "?" — "I don't
  know, does that help?" passes.
- [ ] 51. **Make the contract count honest.** ~8 of "31 contract-checked"
  either don't execute (four `skip_reason` skips report `passed=True`) or
  are `known_broken` (`glob_search`, `project_shell`), and five checks
  assert only "didn't error" (`todo_list`, `cron_list`, `learn_list`,
  `list_capabilities`, `todowrite` — a `cron_list` returning a constant
  empty string passes). Report executed / skipped / known-broken /
  assertion-free as separate numbers, and give the five real assertions.
- [ ] 52. **Re-examine `EXEMPT` rather than trusting its reasons.** R2.3
  requires a reason be recorded and all three have one, but two point at
  judged scenarios that are themselves weak: `send_message`'s substitute
  sends exactly one message so it can never reach the rate limit, and
  `news_summary` can't see fabrication (task 43). An exemption should name
  a substitute *and* be re-checked when that substitute changes.

### The infrastructure spine — no coverage in any layer

- [ ] 53. **Nothing enters the app's lifespan.** Seven startup hooks
  (`_start_mcp`, `_stop_mcp`, `_start_cron_scheduler`, `_start_event_pruner`,
  `_start_history_pruner`, `_populate_context_windows`, `_run_boot_probe`)
  are all `# pragma: no cover`, and the e2e conftest documents *not*
  entering lifespan as a feature. No test in the repo starts the app the
  way production does. Consequence for the judged suite specifically:
  `httpx.ASGITransport` never fires startup, so **MCP servers are never
  spawned** — the report describes a tool surface the operator's real
  gateway doesn't have, and `unsupported` can't detect a tool they have
  and the harness didn't. This is the parent of several items below.
- [ ] 54. **MCP has never spawned a real subprocess.** Every test in
  `test_mcp.py` patches `create_subprocess_exec`; the boot hook is
  uncovered. In production MCP tools also push past the 40-tool capability
  block cap, truncating it — which would change the prompt for all 14
  scenarios.
- [ ] 55. **Cost accounting is never asserted end to end.** `estimate_cost`
  is unit + property tested and `fitt cost` is tested against a
  hand-written log file, but no test drives an HTTP chat request and
  asserts a cost was computed and logged. Deleting the call site in
  `chat.py` passes the whole suite.
- [ ] 56. **The audit chain is never verified over real turns.**
  `AuditLog` is thoroughly tested in isolation (including tamper
  detection); `verify()` has never run against a log produced by real
  turns, and `fitt audit verify` has no test. This is the security story.
- [ ] 57. **The capability-gap log's wiring is untested**, and
  `fitt capability-gaps` has no test — Principle 8's actual mechanism.
  Tests seed the store by hand.
- [ ] 58. **Fallback routing above the router.** `fallback_used` is
  computed and unit-tested with `litellm.acompletion` patched; nothing
  asserts it reaches the response, the structured log or `/v1/status`, nor
  that "no auto-retry on semantic errors" holds through `chat.py`. Phase 1,
  Property 6.
- [ ] 59. **`num_ctx` from config to wire.** Tested at the router with a
  hand-set field; untested that a `config.yaml` value reaches the request,
  and `context_window.py`'s *discovered* windows and the *configured*
  `num_ctx` answer the same question and never meet in a test. The gemma4
  one-token failure is why this matters.
- [ ] 60. **Real SSH execution.** Every integration test stubs
  `run_shell`. `ssh_probe`, `fitt ssh test`, and the `ensure_key` failure
  branch have no wiring coverage — half of the Phase 4 design.
- [ ] 61. **Two of four boot-warning families never asserted at boot.**
  `check_missing_api_keys` and `check_tool_consistency` are tested as pure
  functions only. And the one warning test that *does* check boot asserts a
  patched logger was called — it would pass if the logging config sent that
  record nowhere. Principle 11's whole surface.
- [ ] 62. **`fitt memory reindex` with a real embedder**, plus the
  dimension-mismatch guard that tells operators to reindex. Tested only
  with a fake embedder; the CLI path that builds a real one is untested.
- [ ] 63. **~35 CLI subcommands have no test at all**, including all of
  `session`, all of `project`, all of `ssh`, `config check`, `memory *`,
  `mcp *`, `audit verify`, `capability-gaps`, `tasks`, `scenario run`,
  `profile alias` — and `eval e2e` / `eval contracts` / `eval coverage`
  themselves. Several are an operator's only route to a subsystem.
- [ ] 64. **Open WebUI has no test of any kind.** The one live-fire defect
  (`OPENAI_API_BASE_URL` is PersistentConfig, so compose env is decorative
  after first boot) is documented prose with no regression guard.
- [ ] 65. **`/v1/models` is served without auth**, exposing the alias list
  and `fitt_*` extension fields (model ids, backends). A test asserts the
  200-without-header as correct behaviour; nothing asks whether it should
  be. Decide deliberately, then pin the decision.

### Gate tested, component tested, wire between them untested

The shape of task 46, found in four more places.

- [ ] 66. **Per-client approval overrides.** `project_shell`'s
  `webui: BLOCK` baked-in default lives only at the `create_app` call
  site. Nothing sends `X-FITT-Client: webui` at `/v1/chat/completions` and
  asserts the resolved bucket. Dropping `per_client_defaults=` would pass
  everything — and the e2e conftest, which hardcodes `client_tag="webui"`,
  exercises ASK for that tool, contradicting the baked default.
- [ ] 67. **The send_message rate limiter's construction.** The production
  ceiling (60s / 10) is hardcoded at the call site and the
  `tools.send_message.window_secs` config override is never asserted.
  Doubly invisible: EXEMPT from contracts, and the judged scenario sends
  one message.
- [ ] 68. **The retrieval wiring block.** Untested that binding
  `memory.embedding_alias` registers `memory_search`, that a bad alias
  degrades to a warning instead of killing boot, and that `MemoryIndexer`
  actually receives turns. A break here reports as *unsupported* — which
  the coverage design treats as benign.
- [ ] 69. **The Telegram bot's own suite is fake on both sides.** Task 30
  asks whether it's sufficient; the answer should be informed by this:
  every bot test uses `respx` / `_FakeGateway` / `AsyncMock`, and the
  gateway-side approval e2e uses a hand-written approver rather than
  `fitt_telegram_bot.approval`. Each half of the approval feature is
  tested against a stand-in for the other.
- [ ] 70. **History truncation is measured and never surfaced.**
  `truncated_bytes` is computed and property-tested; nothing asserts the
  operator or model is ever told context was dropped — no event, no
  turn-event field, no `/v1/status` counter.

## Judge enhancements (added 2026-08-12)

Salvaged from a withdrawn "frontier explorer" spec. The subsystem framing
was over-reach, but each idea below is a concrete upgrade to the judge we
already have, and the anchoring one is close to free.

- [ ] 34. **Blind mode (un-anchor the judge).** `build_judge_prompt`
  hands the judge the harness's verdict under "Objective outcome
  (deterministic, checked by code)" and labels the snapshot "GROUND
  TRUTH". On the five occasions the *harness* was wrong, it was handed a
  wrong answer presented as authoritative and agreed every time — once
  while Tier 3 showed it the contradicting evidence verbatim. Add a mode
  that withholds the objective verdict and softens the framing to
  "captured by the harness, may be incomplete".
- [ ] 35. **Replay the judge against known-wrong cases.** We have six:
  dropped `tool_calls`, unregistered `memory_search`, lesson leak within
  a scenario, lesson leak across scenarios, wrong cron delivery channel,
  and (2026-08-13, the sharpest) a cron from a *different* scenario
  blamed on a turn whose Tier-1 evidence read `tools: (none)` — the
  judge restated the model's clarifying question inside the sentence
  condemning it for not asking. Store those trajectories as fixtures and
  assert a blind judge flags what the anchored one rubber-stamped. This
  is a regression test *for the judge*, which nothing currently has.
- [ ] 36. **Require citations in the verdict.** The `reasoning` field
  already must state a root-cause hypothesis (Tier 2); extend it to cite
  specific evidence — which iteration, which tool call, which snapshot
  field. Fluent-but-uncited reasoning is what accompanied every wrong
  verdict.
- [ ] 37. **Audit rubrics (missions, not just per-turn grading).** A
  scenario grades one turn against a rubric. Add a scenario shape whose
  rubric is an *investigation*: "does this run show FITT claiming
  something the internals don't support?", "was the answer reachable by
  the channel the scenario intends?". Same judge, same internals,
  different question.
- [ ] 38. **Let the judge ask for more evidence.** Today it gets a fixed
  dump chosen by `--judge-detail`. A second round-trip — "which iteration
  do you want the sent messages for?" — would beat guessing the tier, and
  keeps Tier-3 prompt bloat off runs that don't need it.
- [ ] 39. **Feed known issues for dedupe.** `_KNOWN_ISSUES` exists but is
  hand-maintained; source it from `docs/observed-issues.md` slugs so the
  judge stops re-reporting fixed defects and spends attention on new
  ground.
