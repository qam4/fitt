# Phase 4 — Agentic Tools: Requirements

## Background

Through Phase 3.5, FITT is a gateway that routes chat through
aliases, remembers identity and session history, and exposes three
interfaces (IDE via Continue, Telegram, Open WebUI). The model can
talk. It can't *do* anything. When you ask "read the repo," it
replies "I can only output text."

Phase 4 changes that: FITT exposes a tool system the model can call.
Files get read, code gets edited, tests get run, HTTP requests get
made. Each tool call flows through an approval-and-audit pipeline so
the agent can work on your behalf without going rogue.

One architectural bet matters above all others:

**Execution follows the project.** FITT's hub is always-on and
orchestrates everything, but the work happens where the code lives
— which might be the hub itself, a laptop, a cloud dev box, or
anywhere reachable by SSH. The hub never pretends to have every
project's files; it asks the execution host for them.

A second bet, smaller but load-bearing:

**Continue brings its own tools.** The IDE case is solved — once
`capabilities: [tool_use]` is declared in Continue's config, it
passes its own tool definitions in the request. FITT appends
session-aware tools (spec tools, cron tools added in Phase 4.5,
lessons tools added in Phase 5) and forwards the merged set.
Telegram and Open WebUI have no client-side tools, so they rely on
FITT's inline set for everything.

## Why now

Phases 1-3.5 built the substrate — stateless routing, memory,
sessions, Telegram, Open WebUI, portable Docker hub. Without
tools, FITT is a chat proxy with memory. Useful but not
differentiated from talking to Claude directly.

With tools, the non-IDE interfaces (Telegram, WebUI) become
genuinely useful because the model can *act*. The "read the repo"
example from live testing is the moment this mattered: the model
wanted to help and couldn't. Phase 4 makes it possible.

It also unlocks Phase 4.5 (cron needs tools to run), Phase 5
(lessons `learn_add` is a tool), and Phase 6 (the spec-runner
coordinates tool calls per step).

## Goals

1. **Inline core tools** for files, search, git, tests, HTTP —
   enough for the non-IDE clients to be useful on day one.
2. **MCP integration** for the long tail of third-party tools
   (Slack, Jira, Home Assistant, etc.) without FITT reimplementing
   them.
3. **SSH-backed execution.** Tools that touch a project's files or
   shell run via SSH on the project's declared host. Hub-local
   projects execute locally.
4. **Tool forwarding** that appends to client-supplied tools
   instead of replacing. Continue keeps its own toolkit; FITT adds
   spec-awareness.
5. **Four-bucket approval** per tool (auto / ask / trust-session /
   yolo / block) with per-client defaults and per-tool overrides.
6. **Audit trail** of every tool invocation in an append-only log
   with HMAC chaining so tampering is detectable.
7. **Capability awareness** so the model is honest when it's asked
   to do something it can't do, and the gap is logged for review.
8. **Project registry** so tools know where projects live and
   what's allowed under them (read/write scope).

## Non-goals (deferred to later phases)

- **Cron, proactive notifications, `send_message` tool.** Phase 4.5.
- **Lessons and lesson tools.** Phase 5.
- **Spec-runner / task execution engine.** Phase 6.
- **Vector/semantic memory.** Phase 7+.
- **Admin web UI.** Phase 7+.
- **Subagents, parallel execution, heartbeat loop.** Phase 9+.
- **Script hooks on chat lifecycle events.** Not planned in the
  current roadmap; approval policy + audit + capability awareness
  cover the practical cases.
- **Custom mode / agent system** (pre-configured personas with
  pruned tool sets). Deferred until we feel the lack.
- **Full general shell on arbitrary commands.** Phase 4 ships
  curated shell-adjacent tools (`run_tests`, `git_*`, `grep_repo`).
  General shell in a per-session container is a later possibility.

## User stories

### U1 — Read the repo from Telegram

> As a Telegram user, I want to ask "read the README of project
> `home-ai-cluster`" and have FITT fetch the file contents and
> summarise them, so I can browse my projects from my phone.

Acceptance:
- FITT has a `read_file` tool registered.
- The tool executes on the project's declared host (via SSH if
  needed).
- The tool is `auto`-approved for Telegram (read-only, low risk).
- Audit log records the call.
- The model incorporates the file contents and replies.

### U2 — Edit a file with approval

> As a Telegram user, I want to ask "rename variable `x` to
> `count` in `config.py`" and get an approval prompt before the
> edit happens, so I don't lose control over what gets written.

Acceptance:
- FITT has an `edit_file` tool registered.
- Policy for `edit_file` via Telegram is `ask`.
- An approval message arrives in Telegram with an inline keyboard
  (Approve / Reject / Trust session).
- On Approve, the edit executes (via SSH on the project host).
- Audit log records both the request and the decision.
- If the approval times out (default 2h), the call auto-rejects
  and logs a timeout outcome.

### U3 — Run tests

> As a Telegram user, I want to ask "run the gateway's tests" and
> see the test output in my chat.

Acceptance:
- FITT has a `run_tests(project_name)` tool.
- The project registry says project `home-ai-cluster` has a test
  command of `uv run pytest -q` (or similar, configurable).
- Policy is `ask` for Telegram (shell-adjacent).
- On approval, the command runs via SSH on the project host.
- Output is streamed back to Telegram (or posted in one chunk if
  short enough).
- Audit log records the invocation.

### U4 — Continue keeps its tools in Agent mode

> As an IDE user with Continue in Agent mode, I want my editor's
> normal file tools (`read_file`, `edit_existing_file`,
> `run_terminal_command`) to keep working when routed through FITT,
> without FITT hijacking them.

Acceptance:
- When an incoming chat request carries a `tools` array, FITT does
  not strip or replace it.
- FITT appends its own spec-aware tools (`spec_read`,
  `spec_next_task`, `spec_mark_task`, `spec_list`) to the array.
- The model sees both sets.
- Tool calls the model makes are routed by name: if the name
  matches a FITT-registered tool, FITT executes it; otherwise the
  call is returned to the client to execute (standard OpenAI
  function-calling semantics).

### U5 — Capability awareness

> As any user, when I ask FITT to do something it cannot do, I
> want it to say what's missing and what tool would need to exist,
> so I know whether to add it.

Acceptance:
- Every session's system prompt includes a generated
  "Capabilities" block listing currently-loaded tools with a
  one-line description each.
- The prompt instructs the model to, when a request needs a tool
  it doesn't have, reply in a standard format: *"I'd need a tool
  to X. Consider adding [suggestion]."*
- A middleware parses the model's reply for this pattern and
  appends an entry to `$FITT_HOME/capability_gaps.log` (timestamp,
  session, originating client, needed capability, suggestion).
- `fitt capability-gaps` CLI reads the log, groups by frequency,
  prints a ranked list.

### U6 — Add a project

> As a user, I want to register a new project with FITT so that
> its tools know where the project lives and what's allowed.

Acceptance:
- `fitt project add <name>` CLI. Prompts for: path on the
  execution host, ssh host (empty = hub-local), test command,
  build command (optional).
- `fitt project list` shows registered projects.
- `fitt project remove <name>`.
- Registry persisted in `$FITT_HOME/projects.yaml`.
- Changes hot-reload (no gateway restart needed) via file watch.

### U7 — Audit trail

> As a user, I want to inspect every tool call FITT made in the
> last week so I can trust that it wasn't doing things I didn't
> approve.

Acceptance:
- Every tool invocation appends a JSON line to
  `$FITT_HOME/audit.jsonl` with: timestamp, session ID, client,
  tool name, arguments (redacted if they contain secrets),
  approval decision, outcome (success/error), HMAC over the prior
  chain.
- `fitt audit verify` validates the HMAC chain end-to-end and
  reports tampering.
- `fitt audit tail [--since] [--tool] [--session]` reads and
  filters the log.
- Secrets redaction: the logger sanitises any argument whose key
  matches a list of sensitive patterns (`password`, `token`,
  `secret`, `key`, etc.) before writing.

### U8 — Block destructive operations

> As a user, I want certain operations to be blocked outright, no
> matter which client asked or what the policy says, so an LLM
> prompt injection can't talk FITT into `rm -rf /`.

Acceptance:
- A deny-list lives in Python code (not config, not user-editable).
- Covers at minimum: `rm -rf` patterns, `git push --force`, `git
  reset --hard origin`, pipe-to-shell from curl/wget, `chmod 777`
  on tree roots, deleting `.git/` directories, `dd if=... of=/dev/`,
  common destructive database commands.
- Any tool invocation whose command string or arguments match the
  deny list fails with a clear error, gets audited as a blocked
  call, and is never executed — regardless of approval state.

### U9 — Per-client approval defaults

> As a user, I want the approval defaults to differ by which
> client made the request, so the IDE doesn't page me for every
> edit while Open WebUI can't run shell commands.

Acceptance:
- Bearer tokens in `secrets.yaml` are tagged with a `client:` label
  (`ide`, `telegram`, `webui`, `cli`).
- The gateway identifies the client from the token used.
- Default policies per client documented in `config.yaml` (and
  overridable per tool).
- If a request presents a token without a `client:` tag, treat as
  the least-trusted client (`webui`).

### U10 — Missing project / missing tool errors

> As a user, when the model calls a tool on a project I haven't
> registered, or a tool that doesn't exist, I want a clear error
> back (not a crash), so I can register the project or install the
> missing tool.

Acceptance:
- Tool call for an unregistered project returns a structured error
  to the model: `UnknownProject(name, available=[...])`.
- Tool call for an unregistered tool returns
  `UnknownTool(name, available=[...])`.
- Both logged in the audit trail as `outcome=error`.
- Model sees the error and replies helpfully.

## Scope boundaries

**In scope:**

- Project registry (schema, YAML file, CLI, hot-reload).
- Per-client token tagging (schema change in `secrets.yaml`).
- Tool registry (inline + MCP under one view).
- Inline tool implementations: `read_file`, `write_file`,
  `edit_file`, `list_directory`, `grep_repo`, `glob_search`,
  `git_status`, `git_diff`, `git_commit`, `run_tests`, `http_get`,
  `spec_read`, `spec_next_task`, `spec_mark_task`, `spec_list`,
  `list_capabilities`.
- SSH execution backend (dispatches tool operations to the
  project's host when needed).
- MCP client + server supervisor (spawn, probe, expose tools).
- Approval middleware with four buckets + per-client defaults +
  per-tool overrides.
- Telegram approval UI (inline keyboard) — the fallback approval
  surface for every non-native client.
- Hardcoded deny list (module + tests).
- Audit log with HMAC chain and verify CLI.
- Capability awareness (system prompt injection + gap logging).

**Out of scope (deferred):**

- Cron, proactive notifications, `send_message` — Phase 4.5.
- Lessons, `learn_add`, decaying history — Phase 5.
- Spec-runner — Phase 6.
- Vector memory — Phase 7+.
- Admin UI — Phase 7+.
- Subagents, heartbeat — Phase 9+.
- General shell (arbitrary commands) — Phase 9+ or never.
- Script hooks on lifecycle events — not currently planned.
- Windows NSSM install path for the new tool layer — the Windows
  legacy install doesn't see Phase 4 updates; it stays as it is.

## Risks and open questions

### R1 — SSH latency

**Risk:** Each tool call that dispatches via SSH adds 100-300ms.
For a chatty request (read 5 files, grep, run tests), this adds
up to seconds of slowdown versus hub-local execution.

**Decision:** accept for Phase 4. Task granularity (minutes) masks
the latency; interactive chat may feel slower but still usable. If
it bites, Phase 7+ can add a persistent SSH session pool or a
satellite-side runner daemon.

### R2 — Smaller models calling tools badly

**Risk:** Qwen 2.5-Coder at 14B handles tool calling less
reliably than cloud frontier models. It may call wrong tools,
fabricate arguments, or loop.

**Decision:**
- Ship an eval harness that runs common tool-using prompts across
  all configured aliases and scores reliability.
- Routing policy downgrades weak aliases to a minimal tool set
  (config-driven allowlist per alias).
- Cycle detection in the tool dispatcher: same tool called with
  the same arguments 3 times in a row fails with "loop detected."

### R3 — Prompt injection through file contents

**Risk:** A malicious file (or an attacker-controlled one pulled
via `http_get`) contains text that instructs the model to do
something harmful. The model follows the instruction.

**Decision:**
- Deny list catches the obvious destructive operations.
- All write operations require approval except on the `ide` client
  (where the human is actively watching).
- `write_file` / `edit_file` scope is limited to registered
  project paths.
- Phase 4 doesn't try to fully solve prompt injection. It relies
  on approval + deny list as the safety net.

### R4 — MCP server crash storms

**Risk:** A buggy MCP server crashes on every tool call. The
supervisor restarts it. It crashes again. Infinite loop consumes
resources.

**Decision:** exponential backoff (1s, 2s, 4s, ..., cap 5min).
After N consecutive failures (default 5), the server stays dead
until the user manually intervenes (`fitt mcp restart <name>` or
gateway restart).

### R5 — Secrets in tool arguments

**Risk:** A tool call passes a token / password / API key as an
argument. The audit log records it. Now the audit log contains
secrets.

**Decision:** a sanitiser runs before every audit write. Regex-
based redaction of common secret patterns (keys with names
containing `password`, `token`, `secret`, `key`, `auth`, `credential`;
values matching common API-key formats). Redacted values replaced
with `<REDACTED>`. Tested with a corpus of real argument shapes.

### R6 — Approval routing when the origin has no UI

**Risk:** A request from a custom client that has no native
approval UI (or Open WebUI which has no practical way to surface
an inline approval button). How do we get the user's answer?

**Decision:** **Telegram is the universal fallback approval
surface.** Any client without a native approval capability routes
its approvals to Telegram. Users are expected to have the
Telegram bot configured; if they don't, `ask`-bucket tool calls
from non-Telegram clients are effectively dead (logged as
timeout → reject).

### R7 — The model doesn't understand capabilities

**Risk:** We inject a capability summary into the system prompt,
but the model ignores it and calls tools that don't exist. Or it
calls a tool with bad arguments because the description was
ambiguous.

**Decision:** accept for v0. This is the fundamental
tool-calling reliability problem. Measured by the eval harness
(R2). If a model is too unreliable, downgrade its toolset and
route complex work elsewhere.

## Success criteria

Phase 4 is done when:

1. On the author's hub, the Telegram bot can successfully:
   - Read a file from a registered project (auto-approved).
   - Edit a file with Telegram approval working (inline keyboard
     clickable, approve/reject round-trips cleanly).
   - Run tests and see output.
2. On the author's laptop, Continue in Agent mode can call its
   own tools (read/edit/terminal) through FITT without
   interference.
3. `fitt audit verify` passes on a log produced by a full day of
   usage.
4. Hardcoded deny list has test coverage for at least 20 patterns.
5. `fitt capability-gaps` works and has captured at least one real
   gap from live use.
6. All existing tests (Phases 1-3.5) still pass.
7. Author has lived with it for 1 week and hit no surprises bad
   enough to warrant reverting.
