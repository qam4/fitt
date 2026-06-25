# Phase 4.7 — `project_shell`: Tasks

**Status:** shipped

Status legend: `[x]` done, `[ ]` not yet.

## 1. Spec promotion

- [x] 1a. Promote the Phase 4.7 inline draft from
       `FITT_ROADMAP.md` to the three-file spec here:
       `requirements.md`, `design.md`, `tasks.md`.
- [x] 1b. Commit the spec separately from the first code
       slice so the rationale has its own change in history.

## 2. Local-shell interpreter probe

- [x] 2a. `gateway/src/gateway/tools/local_shell.py` with
       `ShellInterpreter` dataclass + `LocalShellProbe` class.
       Probe runs `echo probe` under each candidate
       (`bash -lc`, `C:\Program Files\Git\bin\bash.exe -lc`,
       `wsl -- bash -lc`) and returns the first success.
- [x] 2b. Tests (`tests/test_local_shell.py`): resolves to
       `bash` / `git-bash` / `wsl` / `none` in the four
       expected scenarios (monkeypatch subprocess so the
       test doesn't depend on the host shell layout).
- [x] 2c. Wire the probe at `create_app` time; cache the
       result on `app.state.local_shell`. Log the resolved
       interpreter at boot (`fitt.gateway shell.interpreter`).
       `FITT_SKIP_SHELL_PROBE=1` short-circuits the probe for
       tests that don't exercise `project_shell`.

## 3. Deny-list additions

- [x] 3a. Add patterns to `gateway/src/gateway/tools/deny_list.py`:
       `rm -rf $FITT_HOME`, `rm -rf $HOME/.fitt`, `rm -rf ~/.fitt`,
       `git clean -fdx` (the phase's baseline additions). The
       FITT-specific patterns order BEFORE the broader `$HOME`
       ones so the specific label ("wipes identity, history,
       audit") wins over the generic home-directory label.
- [x] 3b. Positive + negative tests for each new pattern in
       `tests/test_deny_list.py`. Negatives cover benign
       siblings (`FITT_HOME=... cmd`, `ls ~/.fitt`,
       `git clean --dry-run -fd`, `git clean -fd`).

## 4. `project_shell` tool

- [x] 4a. `gateway/src/gateway/tools/project_shell.py` with
       `build_project_shell_tool()`. Returns a `Tool` with
       `shell_command_for` wired to the `command` arg so the
       existing deny-list hook in `approval.py` fires before
       dispatch.
- [x] 4b. Schema validation, project lookup, argv build
       (`bash -lc` locally / SSH unchanged), timeout wiring
       (default 120s, max 1800s).
- [x] 4c. Tool emits the `tool_executed` event after
       dispatch; fail paths emit the same kind with
       `timed_out` / exit-code metadata.
- [x] 4d. Unit tests (`tests/test_tools_project_shell.py`):
       schema, local argv, SSH argv, event on success,
       event on timeout, no-shell-available error path.

## 5. Approval prompt: widen cap for `project_shell`

- [x] 5a. `approval._summarise_args` special-cases
       `tool_name == "project_shell"`: show `command` up to
       1000 chars, truncate with `(truncated; N extra chars)`
       if longer, flag the truncation so the user sees it.
- [x] 5b. Other tools continue to use the existing 200-char
       cap.
- [x] 5c. Tests in `tests/test_approval.py`: short command
       (verbatim), medium command (~500 chars, verbatim),
       long command (1500 chars, truncated + flagged),
       other tools unaffected, `timeout_secs` visible
       alongside project+command.

## 6. `tool_executed` event kind

- [x] 6a. `EventEntry.kind` taxonomy docstring updated to
       include `tool_executed`.
- [x] 6b. Gateway emits the event from
       `project_shell._impl` with meta.tool / project /
       command / exit_code / duration_ms / timed_out. Title
       carries `ran`/`FAILED`/`TIMED OUT` prefix so
       operators scanning `fitt inbox` see the outcome at a
       glance.
- [x] 6c. Body = stdout + stderr separated by `--- stderr ---`;
       "(no output)" marker when both are empty.

## 7. Per-client policy defaults

- [x] 7a. `ToolRegistry.register` grows an optional
       `per_client_defaults: dict[str, ApprovalBucket]` kwarg
       and stores it in `_per_tool_baked_in`.
- [x] 7b. `resolve_bucket` consults the per-tool baked-in
       defaults as layer 4 of the existing chain (per-call
       → per-tool-policy → per-tool-wildcard →
       **per-tool-baked-in** → tool.default → client default
       → global). Operator config still wins.
- [x] 7c. `project_shell` registers with
       `{cli: ask, telegram: ask, ide: ask, webui: block}`.
- [x] 7d. Tests: operator config overrides, baked defaults
       apply when no config, missing-client falls through,
       `unregister` clears stale defaults.

## 8. Telegram push formatter

- [x] 8a. `telegram-bot/src/fitt_telegram_bot/events_push.py`
       gets a branch for `tool_executed`. Glyphs: `▶` for
       success, `❌` for non-zero exit, `⏱️` for timeout.
       Body preserves stdout + stderr; "(no output)" marker
       when both are empty.
- [x] 8b. Extend `telegram-bot/tests/test_events_push.py`
       with `tool_executed` cases (success, failure, timeout,
       empty output, missing title).

## 9. E2E lifecycle test

- [x] 9a. `gateway/tests/e2e/test_project_shell_lifecycle.py`
       — stubbed LLM emits `project_shell` call; approver
       approves; assert `tool_executed` event lands with
       expected metadata. Uses a `telegram_approver` fixture
       tagged for telegram client so the bucket resolution
       reaches `ask` (Open WebUI default is `block`).
- [x] 9b. Rejected-path variant: approver rejects; assert
       NO `tool_executed` event; chat turn completes with
       the rejection visible to the model.
- [x] 9c. Deny-list variant: `rm -rf $FITT_HOME` is blocked
       by the middleware before the approver sees anything.
       No backend invocation, no `tool_executed` event.

## 10. `fitt audit tail -f`

- [x] 10a. Added `-f` / `--follow` flag to the existing
       `fitt audit tail` command. In follow mode, after
       printing the initial window the command re-reads the
       log every `--poll-interval` seconds (default 0.5s)
       and prints new entries as they land. Filters
       (`--tool`, `--session`, `--since`) continue to work
       against streamed output.
- [x] 10b. Handles SIGINT (Ctrl-C) cleanly — prints
       `interrupted.` in dim style and exits 0 without a
       stack trace.
- [x] 10c. Tests in `tests/test_cli_audit_tail.py`:
       non-follow empty/populated/filtered paths, `-f`
       flag presence, `_print_audit_entry` formatter for
       both ok and error entries. Follow loop itself is
       spot-checked via the formatter unit test rather
       than driven through CliRunner (can't cleanly
       interrupt); full streaming behaviour covered by
       Task 12 live validation.

## 11. Roadmap pointer update

- [x] 11a. Flip the `*Full spec: ... (to be written when
       this phase starts).*` line at the bottom of the
       Phase 4.7 section in `FITT_ROADMAP.md` to say
       "Spec promoted 2026-05-08; implementation in
       `.kiro/specs/phase4.7-project-shell/`."
- [x] 11b. Mark Phase 4.7 as DONE. Live validation
       completed 2026-05-08: pipes over SSH, failure-path
       event emission, approval-UI cleanup, audit-log
       tracking all verified interactively. Deny list
       covered by unit + integration tests; no live fire
       observed because the active model refuses obvious-
       dangerous patterns before emitting a tool call.

## 12. Live validation

(Manual.)

- [x] 12a. From Telegram: compound-command over SSH
       (`ls -la | head -n 5` against the home-ai-cluster
       satellite). First attempt caught the original
       Git-Bash-over-SSH `shlex.join` bug; fixed in
       `4df31fa` (wrap in `sh -c`); retested green.
- [x] 12b. Failure path: `git show-ref myref` → exit=1 →
       `❌ FAILED` event lands alongside the model's
       natural-language reply.
- [x] 12c. Deny list — unit + integration coverage only.
       Live fire against `rm -rf $FITT_HOME` and
       `git push --force origin main` did not exercise our
       deny list because the model refused on its own
       before emitting a tool call. Audit log confirmed
       no `project_shell` attempt for either prompt.
       Observation noted; machinery ready for future
       models that are less conservative.
- [x] 12d. Approval UI cleanup verified: buttons clear
       on decision, replaced by `V Approve` text as
       designed in Phase 4.5.
- [x] 12e. Two UX observations captured in the roadmap's
       new "UX backlog" section (approval-prompt ordering;
       double-message for interactive shell calls).
       Deferred; not shipping-blockers.

## Definition of done

- Tool exists, deny-list extended, event emitted, per-client
  defaults wired, probe runs at boot.
- Threat model + non-goals in `design.md` per U6.
- `fitt audit tail -f` shipped.
- `uv run pytest -q` green across gateway + telegram-bot.
- E2E lifecycle test green.
- Roadmap pointer updated.
- Two weeks of active use with at least one real `git pull`
  / `npm install` per week — if by then the deny list hasn't
  caught anything real AND the approval prompt hasn't
  surfaced anything the operator wanted to block, bump the
  retrospective note in `design.md` reading "actually useful
  in practice; keep going." If either of those fired, we
  may want to revisit buckets or deny patterns before Phase
  5.
