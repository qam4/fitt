# Phase 4 — Agentic Tools: Tasks

Implementation order keeps the tree green at every commit. Each
top-level group is a reviewable commit.

Status legend: `[x]` done, `[ ]` not yet.

## 1. Project registry

- [x] 1a. Data model: `Project` dataclass with `name`, `ssh_host`,
       `path`, `test_command`, `build_command`. YAML schema
       validated via Pydantic.
- [x] 1b. `ProjectRegistry` class: load from
       `$FITT_HOME/projects.yaml`, look up by name, list, add,
       remove.
- [ ] 1c. File watcher: hot-reload on YAML edit via `watchfiles`.
       Malformed entries log a warning; valid entries stay loaded.
- [x] 1d. CLI: `fitt project add <name>` (interactive prompts),
       `fitt project list`, `fitt project remove <name>`.
- [x] 1e. Wire into gateway startup: load registry, start watcher.
- [x] 1f. Tests: `test_projects.py`.

## 2. Client-tagged tokens

- [x] 2a. Extend `AllowedToken` model with optional `client:`
       field. Literal: `ide` | `telegram` | `webui` | `cli`.
- [x] 2b. `Secrets.client_for(token)` helper; fallback to
       `"webui"` for untagged tokens.
- [x] 2c. Update auth middleware to pass client tag into the
       request context.
- [x] 2d. Update `configs/secrets.example.yaml` with tagged
       examples.
- [x] 2e. Tests: token resolution with and without tags.

## 3. Tool registry scaffolding

- [x] 3a. `gateway/tools/_types.py`: `Tool`, `ToolResult`,
       `ApprovalBucket`, `ApprovalDecision`, `ToolContext`
       dataclasses.
- [x] 3b. `gateway/tools/registry.py`: `ToolRegistry.register`,
       `unregister`, `lookup`, `list_all`, `describe_all`.
- [x] 3c. Policy loader: parse `tools:` section of `config.yaml`
       into a `ToolPolicy` object.
- [x] 3d. `resolve_bucket(tool, client, session)` implementing
       the documented precedence chain.
- [x] 3e. Tests: `test_tools_registry.py`.

## 4. Read-only inline tools (no SSH backend yet)

- [x] 4a. `list_capabilities`: returns the tool list as JSON for
       the model.
- [x] 4b. `spec_read(feature_name)`, `spec_list()`, `spec_next_task`,
       `spec_mark_task`. All operate on
       `<project-path>/.kiro/specs/<feature>/`. Hub-local only for
       now; SSH comes in task 5.
- [x] 4c. Register with the tool registry. Default bucket: `auto`.
- [x] 4d. Tests: `test_tools_inline.py`.

## 5. SSH execution backend

- [x] 5a. `gateway/tools/backend.py` with `ExecutionBackend.run_shell`:
       dispatches via `ssh <host> '<cmd>'` when the project has an
       `ssh_host`; local subprocess otherwise.
- [x] 5b. Timeout handling, stderr capture, returncode.
- [x] 5c. SSH key location: document
       `$FITT_HOME/ssh/id_ed25519`. Gateway reads via
       `SSH_AUTH_SOCK` from agent OR key file. Pick one.
- [x] 5d. Tests: `test_tools_ssh_backend.py`. Mocks
       `asyncio.create_subprocess_exec`.

## 6. File-access inline tools

- [x] 6a. `read_file(project, path)` uses the SSH backend.
- [x] 6b. `list_directory(project, path)`.
- [x] 6c. `grep_repo(project, pattern, path_filter?)`.
- [x] 6d. `glob_search(project, pattern)`.
- [x] 6e. Path-resolution safety: no `..` escape; absolute paths
       rejected outside the project root.
- [x] 6f. Tests.

## 7. Git read-only tools

- [x] 7a. `git_status(project)`.
- [x] 7b. `git_diff(project, ref?, path?)`.
- [x] 7c. Tests.

## 8. Approval middleware

- [ ] 8a. `gateway/approval.py` scaffolding: `ApprovalMiddleware`
       with `check(tool, args, context)`. Returns
       `ApprovalDecision`.
- [ ] 8b. Integration with the tool registry's `resolve_bucket`.
- [ ] 8c. Session-trust tracking (in-memory dict, per-session
       trusted tools).
- [ ] 8d. YOLO tracking with per-client expiry (30 min for
       telegram/webui, 6 h for ide/cli).
- [ ] 8e. Hook approval into tool execution path: before dispatch,
       call `approval.check`. Block and not-executed decisions
       short-circuit.
- [ ] 8f. Tests with mock client contexts.

## 9. Telegram approval UI

- [ ] 9a. Telegram bot: `approval_callback_query` handler.
       Parses callback-data, resolves a futures table.
- [ ] 9b. `TelegramNotifier.ask(tool, args, timeout_secs)`:
       sends a formatted message with inline keyboard, returns
       `asyncio.Future` resolved by the callback handler.
- [ ] 9c. 2-hour default timeout; auto-rejects on expiry.
- [ ] 9d. Respects allowlist: only the single allowlisted user can
       approve.
- [ ] 9e. Tests: mock Telegram client; verify keyboard shape and
       callback handling.

## 10. Write-gated inline tools

- [ ] 10a. `write_file(project, path, content)`.
- [ ] 10b. `edit_file(project, path, old_str, new_str)`. Verifies
       the old string appears exactly once before replacing.
- [ ] 10c. `git_commit(project, message)`. Implied `git add -A`.
- [ ] 10d. Default bucket `ask` per config; auto for `ide` client
       via per-client override.
- [ ] 10e. Tests.

## 11. Shell-adjacent tools

- [ ] 11a. `run_tests(project)`. Uses `project.test_command`.
- [ ] 11b. `http_get(url)`. Respects `deny_hosts` from config.
- [ ] 11c. Tests.

## 12. Deny list

- [ ] 12a. `gateway/tools/deny_list.py` with the hardcoded
       `DENY_PATTERNS` list and `check(cmd)` function.
- [ ] 12b. Tests: one per pattern, verifying both positive (catches
       destructive form) and negative (doesn't over-match benign
       forms).
- [ ] 12c. Wire into approval middleware: deny check happens
       before bucket resolution.
- [ ] 12d. Tests: approval middleware + deny list integration.

## 13. Audit log

- [ ] 13a. `gateway/audit.py`: `AuditEntry` dataclass, `AuditLog`
       class with append + verify.
- [ ] 13b. HMAC chaining: key stored at
       `$FITT_HOME/audit.key` with 0600 perms, generated on first
       use.
- [ ] 13c. Secret redaction helper.
- [ ] 13d. Hook audit into tool execution path.
- [ ] 13e. CLI: `fitt audit verify` and `fitt audit tail` with
       filters (`--since`, `--tool`, `--session`).
- [ ] 13f. Tests: chain integrity, tamper detection, redaction.
       Property-based test for chain.

## 14. MCP client

- [ ] 14a. `gateway/tools/mcp_client.py`: subprocess spawn with
       pipe I/O; JSON-RPC framing.
- [ ] 14b. Server supervisor: crash detection, exponential backoff,
       give-up after 5 consecutive failures.
- [ ] 14c. Tool discovery at startup: `initialize` + `tools/list`,
       register tools with `mcp.<server>.<tool>` prefix, default
       bucket `ask` (wildcards from config).
- [ ] 14d. Tool invocation path: `tools/call` JSON-RPC, result
       back to the model via the standard tool-result message.
- [ ] 14e. `fitt mcp list`, `fitt mcp restart <name>` CLI.
- [ ] 14f. Tests: mock MCP server fixture, full round-trip.

## 15. Capability awareness

- [ ] 15a. `gateway/capabilities.py`: `build_capability_block`
       generating the system-prompt section.
- [ ] 15b. Inject into the system prompt next to identity and
       session memory. Cap at a reasonable size.
- [ ] 15c. Gap detection regex on the final model reply.
- [ ] 15d. `CapabilityGapLog`: append to
       `$FITT_HOME/capability_gaps.log`.
- [ ] 15e. CLI: `fitt capability-gaps` prints a ranked list.
- [ ] 15f. Tests.

## 16. Tool forwarding in chat handler

- [x] 16a. Update `chat.py` to detect and preserve
       client-supplied `tools` array.
- [x] 16b. Append FITT's registered tools.
- [~] 16c. Name-based dispatch: FITT-owned → execute locally;
       client-owned → return to the client.
       *Partial: FITT-owned tools execute; client-owned names
       currently return as a "tool not registered" error to the
       model. Full forward-back-to-client behaviour awaits the
       Continue IDE integration test (task 17b).*
- [x] 16d. Tool-call loop bounded at 10 iterations (configurable).
- [x] 16e. Tests: `test_chat_tool_forwarding.py`.

## 17. Integration tests

- [ ] 17a. End-to-end Telegram → tool call → execution → reply.
       Stubbed LLM and SSH.
- [ ] 17b. End-to-end Continue-style IDE request: client tools
       preserved, FITT tools appended, dispatch correctly split.

## 18. Docs

- [ ] 18a. Gateway README: new sections on tools, project
       registry, approval model.
- [ ] 18b. Quickstart: new step for `fitt project add` after
       install.
- [ ] 18c. `.kiro/steering/project-overview.md`: note the new
       architecture (tools, ssh backend, project registry).
- [ ] 18d. Mention the SSH key requirement on the execution host
       and the one-time `ssh-copy-id` setup.

## 19. Live validation

(Manual, done by the author.)

- [ ] 19a. On the QNAP hub, register `home-ai-cluster` as a
       hub-local project.
- [ ] 19b. Register `retro-ai` (or equivalent) on a satellite
       with `ssh_host`.
- [ ] 19c. From Telegram: `read README.md` of home-ai-cluster.
       Auto-approved, returns content.
- [ ] 19d. From Telegram: `edit config.py` of retro-ai to change
       a string. Approval prompt arrives, approve, verify the
       edit landed on the satellite.
- [ ] 19e. From Telegram: run tests on home-ai-cluster.
- [ ] 19f. From VS Code Continue: verify Agent mode still works
       with FITT's model routing (no interference from the
       gateway's new tool forwarding).
- [ ] 19g. Trigger a `list_capabilities` call via Telegram and
       verify the capability block matches the registered set.
- [ ] 19h. Intentionally ask for something FITT can't do, verify
       a gap appears in `capability_gaps.log`.
- [ ] 19i. Run `fitt audit verify` after a day of use; passes.

## Definition of done

- Every required `[ ]` above completed.
- `uv run pytest -q` passes in `gateway/` and `telegram-bot/`.
- Ruff + mypy clean on the gateway package.
- The live-validation checklist (19a-19i) all green.
- Author has used Phase 4 for 1 week without wanting to revert.
