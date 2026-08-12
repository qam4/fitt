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
- [~] 3. Fixtures: temp FITT home, registered temp project, temp git
  repo, file tree — DONE via `tool_contract_suite.build_project` /
  `init_git_repo` reusing `_fixtures.build_test_config`. Stub HTTP
  server for `http_get` still to do.
- [x] 4. Read-mostly surface: `read_file`, `list_directory`,
  `grep_repo`, `list_capabilities` pass; `glob_search` marked
  known-broken (real Windows defect found by this layer — see task 24).
  `http_get` deferred with task 3's stub server.
- [~] 5. State tools: `todo_list`, `cron_list`, `learn_add` (with a
  side-effect assertion on the lessons store), `learn_list`,
  `learn_remove` DONE. Still to do: `todo_add`/`todo_done`/`todo_remove`,
  `cron_add`/`cron_update`/`cron_pause`/`cron_resume`/`cron_remove`.
- [ ] 6. Write/code surface — coverage only, no investment:
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

- [ ] 8. `exercises_tools` on `TaskScenario` (intent, distinct from
  `requires_tools`) and `EXEMPT` map with reasons. (D1; Property 5)
- [ ] 9. `fitt eval coverage`: registry minus (scenario intent + contract
  checks + exemptions) -> uncovered names + counts. (R2.1-2.3;
  Property 1)
- [ ] 10. Test: registering a tool makes it appear uncovered with no
  other edit. (Property 1)

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
- [ ] 18. Skills loader: a temp skill is offered to the model and
  appears in the capability surface. (R4)
- [ ] 19. Planned-mode orchestration: same scenario via `--mode planned`,
  compared against flat. (R4)
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
- [ ] 23. Regenerate `docs/feature-model-standing.md` with the full set
  and re-measure all three DUTs (`--samples` > 1 for anything cited).


## Roadmap-derived gaps (added 2026-08-12)

*(Not a roadmap phase. The lettered phases above are sections of this
spec's plan; FITT's roadmap phases are numbered — 4.5, 9, 12 — and the
items here are cross-references INTO that roadmap, not a new phase of
it.)*

Phases A-F were scoped from the *tool registry*, which is why they miss
whole features: a capability with no tool (skills) or a different
execution mode (planned) is invisible to a registry-derived count. Mapped
against `FITT_ROADMAP.md`, these ship today and the standing view says
nothing about them:

- [ ] 27. **Skills loader (Phase 4.10).** Shipped, and skills aren't
  tools — the contract layer can't see them. Needs a scenario where a
  temp skill is loaded, appears in the capability surface, and the model
  uses it. Probably also a contract-style check that a malformed
  `SKILL.md` is rejected with a readable error rather than breaking boot.
- [ ] 28. **Lessons applied later (Phase 5).** `learn_*` are
  contract-checked, but nothing tests the *point* of lessons: correct the
  assistant, then see the correction honoured on a later turn. Note the
  global-lessons channel already caused two false verdicts here, so this
  scenario must isolate its fixture (see R5.4).
- [ ] 29. **Planned mode (Phase 12).** `--mode planned` orchestration has
  a spec and no e2e coverage. Cheapest useful form: run one existing
  multi-step scenario both flat and planned, and compare.
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
- [ ] 35. **Replay the judge against known-wrong cases.** We have five:
  dropped `tool_calls`, unregistered `memory_search`, lesson leak within
  a scenario, lesson leak across scenarios, wrong cron delivery channel.
  Store those trajectories as fixtures and assert a blind judge flags
  what the anchored one rubber-stamped. This is a regression test *for
  the judge*, which nothing currently has.
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
