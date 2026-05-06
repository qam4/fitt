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

Slim version landed in `ef7fa49` — enough for the `auto`/`block`
path used by read-only tools today. Session-trust and YOLO state
ship alongside Task 9 when there's a UI to set them.

- [x] 8a. `gateway/approval.py` scaffolding: `ApprovalMiddleware`
       with `check(tool, args, context)`. Returns
       `ApprovalDecision`.
- [x] 8b. Integration with the tool registry's `resolve_bucket`.
- [ ] 8c. Session-trust tracking (in-memory dict, per-session
       trusted tools). Placeholder `trust_session()` /
       `clear_session()` no-ops exist; real state pairs with
       Task 9.
- [ ] 8d. YOLO tracking with per-client expiry (30 min for
       telegram/webui, 6 h for ide/cli). Pairs with Task 9.
- [x] 8e. Hook approval into tool execution path: before dispatch,
       call `approval.check`. Block and not-executed decisions
       short-circuit.
- [x] 8f. Tests with mock client contexts.

## 9. Telegram approval UI

**Design note (added before implementation).** The gateway and the
Telegram bot are two separate processes that need to coordinate to
resolve `ask` / `trust_session` tool calls. Three shapes considered:

- **A. Polling.** Gateway exposes `/v1/approvals/pending` and
  `/v1/approvals/{id}/decide`. Bot polls pending every 500-1000ms,
  surfaces to Telegram, posts decisions back.
- **B. SSE push.** Gateway streams approval events to a long-lived
  bot connection.
- **C. Webhook back to bot.** Gateway POSTs approval requests to a
  bot-owned HTTP endpoint.

**Chosen: A (polling).** Rationale:
- No new network plumbing (reuses the existing gateway HTTP surface).
- Survives bot or gateway restart cleanly — pending approvals live
  on the gateway; bot reconnects and picks up where it left off.
- Polling at 500ms adds ~0ms perceived latency for a feature that
  already has a 2-hour human timeout.
- SSE and webhook shapes earn their complexity only once we need
  sub-500ms push — not today.

**On-disk state.** Pending approvals live in an in-memory dict on
the gateway for v1 (keyed by approval id, value has tool/args/
context/future). Lost on gateway restart. Phase 4.5's event log
is where persistent approval records should go; doing it here
would duplicate work.

**Futures plumbing.** When the chat loop hits an `ask` tool, the
gateway creates an `asyncio.Future`, stores it in the pending
dict under a fresh UUID, and awaits it with a 2-hour timeout.
Bot polls, shows the Telegram prompt, user clicks, bot POSTs the
decision. Gateway's decide endpoint looks up the UUID, resolves
the future, dispatch continues.

### Tasks

- [x] 9a. `gateway/approval.py`: `ApprovalMiddleware` holds a
       pending-approvals dict. `request_approval(tool, args,
       context)` creates the future, stores it, returns
       `(approval_id, future)`. Caller awaits the future.
- [x] 9b. `gateway/approval.py`: `resolve_approval(approval_id,
       decision)` sets the future. Called by the decide HTTP
       handler. Idempotent on already-resolved.
- [x] 9c. Gateway HTTP: `GET /v1/approvals/pending?client=X`
       returns `[{id, tool, args_summary, client, session, age_s}, ...]`.
       Only pending (not yet resolved) entries. Filters by
       client.
- [x] 9d. Gateway HTTP: `POST /v1/approvals/{id}/decide` with body
       `{decision: "approve" | "reject" | "trust_session"}`.
       Resolves the future. Requires the requesting token's
       client tag to match the approval's target client (so the
       ide token can't approve a telegram-bound prompt).
- [x] 9e. Telegram bot: add `ApprovalPoller` task that runs
       alongside the chat handler. `while True: fetch pending,
       post new ones to Telegram, sleep 500ms`. Tracks which
       approval ids it's already surfaced so we don't re-post.
- [x] 9f. Telegram bot: `on_callback_query` handler for the
       inline keyboard buttons. Parses callback-data shape
       `approve:<id>` / `reject:<id>` / `trust:<id>` and POSTs
       to gateway's decide endpoint.
- [x] 9g. Wire the middleware: when `check` returns `ask` /
       `trust_session` and `request_approval` is available,
       await the future (2-hour timeout). Map the resolved
       decision to an `ApprovalDecision`. Keep the "not wired"
       fallback for `yolo` — that comes later.
- [x] 9h. Respects allowlist: only allowlisted Telegram user ids
       see the prompt. Other chat members on a group (if any)
       get nothing. The bot already filters messages this way;
       we just inherit the same check for callback queries.
- [x] 9i. 2-hour default timeout; auto-rejects on expiry with a
       clear detail message. Configurable via
       `tools.approval_timeout_secs`.
- [x] 9j. Tests: approval future lifecycle (pending, resolved,
       timeout), HTTP endpoint auth + client-tag matching, bot
       poller fetches + dedupes, callback handler posts back the
       right shape.

**Known limitation (real-world end-to-end).** The approval
future blocks the chat HTTP request until resolved. Telegram
bot's httpx client times out at ~60s, so the user has to tap
within that window or the chat response never lands — even
though the tool runs correctly on the gateway side. We cap the
approval timeout at 45s by default (configurable via
`tools.approval_timeout_secs`) so the failure mode is
predictable instead of the bot silently disappearing.

The correct long-term fix is detached execution: chat turn
returns a placeholder immediately, tool runs in the background
after approval, result is pushed to Telegram as a new message.
That's the same push channel Phase 4.5 builds for cron +
proactive notifications, so this is not a Phase 4 blocker —
just a known rough edge.

## 10. Write-gated inline tools

- [x] 10a. `write_file(project, path, content)`.
- [x] 10b. `edit_file(project, path, old_str, new_str)`. Verifies
       the old string appears exactly once before replacing.
- [x] 10c. `git_commit(project, message)`. Implied `git add -A`.
- [x] 10d. Default bucket `ask` per config; auto for `ide` client
       via per-client override. (Documented in
       `configs/config.example.yaml`; operators opt in.)
- [x] 10e. Tests.

## 11. Shell-adjacent tools

- [x] 11a. `run_tests(project)`. Uses `project.test_command`.
- [x] 11b. `http_get(url)`. Respects `deny_hosts` from config.
- [x] 11c. Tests.

## 12. Deny list

- [x] 12a. `gateway/tools/deny_list.py` with the hardcoded
       `DENY_PATTERNS` list and `check(cmd)` function.
- [x] 12b. Tests: one per pattern, verifying both positive (catches
       destructive form) and negative (doesn't over-match benign
       forms).
- [x] 12c. Wire into approval middleware: deny check happens
       before bucket resolution.
- [x] 12d. Tests: approval middleware + deny list integration.

## 13. Audit log

- [x] 13a. `gateway/audit.py`: `AuditEntry` dataclass, `AuditLog`
       class with append + verify.
- [x] 13b. HMAC chaining: key stored at
       `$FITT_HOME/audit.key` with 0600 perms, generated on first
       use.
- [x] 13c. Secret redaction helper.
- [x] 13d. Hook audit into tool execution path.
- [x] 13e. CLI: `fitt audit verify` and `fitt audit tail` with
       filters (`--since`, `--tool`, `--session`).
- [x] 13f. Tests: chain integrity, tamper detection, redaction.
       Property-based test for chain.

## 14. MCP client

- [x] 14a. `gateway/tools/mcp_client.py`: subprocess spawn with
       pipe I/O; JSON-RPC framing. *(Landed as
       `gateway/mcp.py` — kept out of `tools/` to avoid a
       circular import cycle with the registry.)*
- [x] 14b. Server supervisor: crash detection, exponential backoff,
       give-up after 5 consecutive failures.
- [x] 14c. Tool discovery at startup: `initialize` + `tools/list`,
       register tools with `mcp.<server>.<tool>` prefix, default
       bucket `ask` (wildcards from config).
- [x] 14d. Tool invocation path: `tools/call` JSON-RPC, result
       back to the model via the standard tool-result message.
- [x] 14e. `fitt mcp list`, `fitt mcp restart <name>` CLI.
- [x] 14f. Tests: mock MCP server fixture, full round-trip.

## 15. Capability awareness

- [x] 15a. `gateway/capabilities.py`: `build_capability_block`
       generating the system-prompt section.
- [x] 15b. Inject into the system prompt next to identity and
       session memory. Cap at a reasonable size.
- [x] 15c. Gap detection regex on the final model reply.
- [x] 15d. `CapabilityGapLog`: append to
       `$FITT_HOME/capability_gaps.log`.
- [x] 15e. CLI: `fitt capability-gaps` prints a ranked list.
- [x] 15f. Tests.

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

## Follow-ups (not in Phase 4 scope)

Deferred from the Phase 4 SSH discussion. Tracked here so they
don't drift, but none block Phase 4 exit. Each is a future
commit of its own; pick them up when the pain actually shows up.

- [ ] F1. **Simplify backend ssh argv.** Move
      `-o StrictHostKeyChecking=accept-new` out of the argv into
      a container-side `$FITT_HOME/ssh/config` (accessed via
      `-F`). Keep `-i` and `-o BatchMode=yes` on the argv — the
      key path varies with `FITT_HOME` across deployments (Docker
      vs native Linux vs native Windows) and BatchMode is a
      backend invariant. The config file is the natural home for
      per-host overrides the user wants to tweak without
      touching FITT code. Requires a Dockerfile change (copy a
      default `config` file into the image), a startup check
      that copies the default to `$FITT_HOME/ssh/config` if
      absent, and an argv change in `backend.py`.

- [ ] F2. **Optional per-project `shell:` field on `Project`.**
      When set (e.g. `"/usr/bin/bash -lc"` or
      `"C:\\Tools\\Git\\usr\\bin\\bash.exe -lc"`), the backend
      wraps the remote command as
      `<shell> 'cd <path> && <cmd>'` instead of passing the
      command straight through. When unset, today's behaviour —
      remote sshd's default shell runs the command. Solves the
      WSL-vs-Git-Bash ambiguity on Windows satellites without
      forcing everyone to touch `DefaultShell`. Adds one field,
      one conditional in `_build_ssh_argv`, one CLI flag on
      `fitt project add`.

- [x] F3. **Upgrade `fitt ssh test` output.** Print the full
      argv that was sent (users can paste it directly into a
      shell for debugging) and detect the remote shell from the
      `uname -a && pwd` output (MSYS = Git Bash, Linux +
      `/mnt/c/` = WSL, Linux + `/home/` = native Linux). Named
      in the CLI output so failure modes like "landed in WSL
      instead of Git Bash" are immediately visible. No backend
      change.

- [ ] F4. **Native-install quickstart.** Sibling doc to today's
      Docker-focused `quickstart.md`. Covers: systemd unit on
      Linux, Windows service via NSSM (or revived
      `install-service.ps1`), direct `uv run fitt serve` for dev
      loops. No code change — the gateway is already
      deployment-neutral; this is pure docs. Probably 1-2 hours.
      Flagged in `project-overview.md` as an intentional future
      direction.
