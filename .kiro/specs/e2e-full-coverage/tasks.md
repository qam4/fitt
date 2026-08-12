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
- [ ] 25. **Widen POSIX-shell discovery** (found while diagnosing 24).
  `local_shell._CANDIDATES` hardcodes `C:\Program Files\Git\bin\bash.exe`,
  so a Git install anywhere else (e.g. `C:\Tools\Git`) reports "no POSIX
  shell" on a machine that has one — which is why every eval run this
  session logged `shell.interpreter_unavailable`. Derive the path from
  `git` on PATH instead of guessing one location.

## Phase B — Coverage report

- [ ] 8. `exercises_tools` on `TaskScenario` (intent, distinct from
  `requires_tools`) and `EXEMPT` map with reasons. (D1; Property 5)
- [ ] 9. `fitt eval coverage`: registry minus (scenario intent + contract
  checks + exemptions) -> uncovered names + counts. (R2.1-2.3;
  Property 1)
- [ ] 10. Test: registering a tool makes it appear uncovered with no
  other edit. (Property 1)

## Phase C — Proactive behaviour (judged)

- [ ] 11. Recording message sink on app.state + `sent_messages` in
  `snapshot_app`. (D3; R3.1)
- [ ] 12. `send_message` scenario ("tell me X on Telegram") with an
  objective check reading the sink. (R3.1)
- [ ] 13. Cron-fires scenario: setup hook pre-creates a job, forced tick,
  assert it ran + delivered via the sink. No sleeping. (D4; R3.2)
- [ ] 14. Cron cancel/pause scenarios using the setup hook, so
  cancelled is distinguishable from never-created. (R3.3)

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
