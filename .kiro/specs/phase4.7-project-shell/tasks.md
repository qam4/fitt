# Phase 4.7 — `project_shell`: Tasks

Status legend: `[x]` done, `[ ]` not yet.

## 1. Spec promotion

- [ ] 1a. Promote the Phase 4.7 inline draft from
       `FITT_ROADMAP.md` to the three-file spec here:
       `requirements.md`, `design.md`, `tasks.md`.
- [ ] 1b. Commit the spec separately from the first code
       slice so the rationale has its own change in history.

## 2. Local-shell interpreter probe

- [ ] 2a. `gateway/src/gateway/tools/local_shell.py` with
       `ShellInterpreter` dataclass + `LocalShellProbe` class.
       Probe runs `echo probe` under each candidate
       (`bash -lc`, `C:\Program Files\Git\bin\bash.exe -lc`,
       `wsl -- bash -lc`) and returns the first success.
- [ ] 2b. Tests (`tests/test_local_shell.py`): resolves to
       `bash` / `git-bash` / `wsl` / `none` in the four
       expected scenarios (monkeypatch subprocess so the
       test doesn't depend on the host shell layout).
- [ ] 2c. Wire the probe at `create_app` time; cache the
       result on `app.state.local_shell`. Log the resolved
       interpreter at boot (`fitt.gateway shell.interpreter`).

## 3. Deny-list additions

- [ ] 3a. Add patterns to `gateway/src/gateway/tools/deny_list.py`:
       `rm -rf $FITT_HOME`, `rm -rf $HOME/.fitt`,
       `git clean -fdx` (the phase's baseline additions).
- [ ] 3b. Positive + negative tests for each new pattern in
       `tests/test_deny_list.py`.

## 4. `project_shell` tool

- [ ] 4a. `gateway/src/gateway/tools/project_shell.py` with
       `build_project_shell_tool(backend, events,
       local_shell_probe)`. Returns a `Tool` with
       `shell_command_for` wired to the `command` arg so the
       existing deny-list hook in `approval.py` fires before
       dispatch.
- [ ] 4b. Schema validation, project lookup, argv build
       (`bash -lc` locally / SSH unchanged), timeout wiring.
- [ ] 4c. Tool emits the `tool_executed` event after
       dispatch; fail paths emit the same kind with
       `timed_out` / exit-code metadata.
- [ ] 4d. Unit tests (`tests/test_project_shell.py`):
       schema, local argv, SSH argv, event on success,
       event on timeout, no-shell-available error path.

## 5. Approval prompt: widen cap for `project_shell`

- [ ] 5a. `approval._summarise_args` special-cases
       `tool.name == "project_shell"`: show `command` up to
       1000 chars, truncate with `(truncated; <n> chars)` if
       longer, flag the truncation so the user sees it.
- [ ] 5b. Other tools continue to use the existing 200-char
       cap.
- [ ] 5c. Tests in `tests/test_approval.py`: short command
       (verbatim), medium command (~500 chars, verbatim),
       long command (1500 chars, truncated + flagged),
       other tools unaffected.

## 6. `tool_executed` event kind

- [ ] 6a. Extend `gateway.events.EventEntry.kind` taxonomy
       docstring to mention `tool_executed`.
- [ ] 6b. Gateway emits the event from
       `project_shell._impl` with meta.tool / project /
       command / exit_code / duration_ms / timed_out.
- [ ] 6c. Body = stdout + stderr, capped via
       `events.telegram_body_cap`.

## 7. Per-client policy defaults

- [ ] 7a. `ToolRegistry.register` grows an optional
       `per_client_defaults: dict[str, ApprovalBucket]` kwarg.
- [ ] 7b. `resolve_bucket` consults the per-tool default
       table as layer 3 of the existing chain
       (per-call → per-tool-policy → per-tool-default →
       tool.default_bucket). No change to operator-config
       precedence.
- [ ] 7c. `project_shell` registers with
       `{cli: ask, telegram: ask, ide: ask, webui: block}`.
- [ ] 7d. Tests: operator config overrides, per-client
       defaults apply when no config, `block` for webui is
       the default.

## 8. Telegram push formatter

- [ ] 8a. `telegram-bot/src/fitt_telegram_bot/events_push.py`
       gets a branch for `tool_executed`. Title:
       `▶ ran project_shell: <command-truncated-30>`. Body:
       stdout+stderr per the cap; "(no output)" marker
       when empty.
- [ ] 8b. Extend `telegram-bot/tests/test_events_push.py`
       with `tool_executed` cases (success, timeout, empty
       output).

## 9. E2E lifecycle test

- [ ] 9a. `gateway/tests/e2e/test_project_shell_lifecycle.py`
       — stubbed LLM emits `project_shell` call; approver
       approves; assert `tool_executed` event lands with
       expected metadata.
- [ ] 9b. Rejected-path variant: approver rejects; assert
       NO `tool_executed` event; assert the chat turn
       completes with the rejection visible to the model.

## 10. `fitt audit tail -f`

- [ ] 10a. Add `-f` / `--follow` flag to the existing
       `fitt audit tail` command. In follow mode, after
       printing the initial window the command re-stats the
       log file every 500ms and prints new entries as they
       land. Filters (`--tool`, `--session`, `--since`)
       continue to work.
- [ ] 10b. Handle SIGINT (Ctrl-C) cleanly — close the file
       and exit 0 without a stack trace.
- [ ] 10c. Test in `tests/test_cli_audit_tail_follow.py`:
       spawn the command against a temp audit file; append
       entries; assert they appear. Use `runner.invoke(...,
       input="\x03")` plus a small helper to drive the
       follow loop, or refactor the follow body into an
       async helper the test calls directly. Whichever is
       simpler to make deterministic.

## 11. Roadmap pointer update

- [ ] 11a. Flip the `*Full spec: ... (to be written when
       this phase starts).*` line at the bottom of the
       Phase 4.7 section in `FITT_ROADMAP.md` to say
       "Spec promoted YYYY-MM-DD; implementation in
       `.kiro/specs/phase4.7-project-shell/`."
- [ ] 11b. Mark Phase 4.7 as IN PROGRESS / DONE as slices
       land (matches the Phase 1/4.5 convention).

## 12. Live validation

(Manual.)

- [ ] 12a. On the NAS, after `docker compose build && up -d`,
       from Telegram: "run `git status` in the fitt repo".
       Tap approve. Verify the `tool_executed` event lands
       as a new Telegram message with stdout visible.
- [ ] 12b. From Telegram: "run `rm -rf $FITT_HOME`". Verify
       the deny-list rejection reaches the model and the
       model re-phrases / gives up rather than retrying.
- [ ] 12c. From Telegram: "run a 5-minute sleep" (30s
       timeout). Verify the tool times out and the event
       body names the timeout.
- [ ] 12d. Open WebUI: ask the same "run git status"
       question; verify the tool is blocked (webui default).
- [ ] 12e. IDE (Continue): set `ide.project_shell:
       trust_session` in config; make two shell calls in
       one session; verify only the first prompts.

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
