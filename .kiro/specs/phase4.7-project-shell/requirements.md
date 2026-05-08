# Phase 4.7 — `project_shell`: Requirements

## Context

Phase 4 shipped narrow, curated tools: `read_file`, `write_file`,
`git_commit`, etc. Every shell-requiring workflow the model
actually wants — `git pull`, `git log -n 5 --oneline`, `npm
install`, `ls dist/`, `find . -name '*.py' | head` — is missing.
Users asking "pull the latest" get "I can't run git pull"
despite every primitive the answer needs (backend, deny list,
approval, audit, event log) already being built.

Phase 4.7 closes the honesty gap by adding one tool —
`project_shell` — that executes an arbitrary shell command
string in a registered project. Guardrails layer over the
existing Phase 4.5 approval + audit + event stack; no new
primitives.

This is also the forcing function for writing the threat model
down. "Arbitrary shell" is a different security conversation
than narrow tools, and the spec is where we state — in prose,
not comments — what the v0 does and doesn't protect against.

## User stories

### U1. The agent can run shell commands

As a FITT user, I want the agent to run shell commands on a
project's execution host so that routine tasks (`git pull`,
`npm install`, `ls dist/`) don't require me to switch to a
terminal.

**Acceptance:**

- **1.1** A `project_shell(project, command, timeout_secs?)`
  inline tool exists. `command` is a string. It's executed as
  `bash -lc <command>` so pipes, globs, redirection, and
  command-chaining all work.
- **1.2** On success the tool returns stdout + stderr + exit
  code in a structured string the model can reason about. On
  timeout it returns a clear `timed_out` message with the
  duration.
- **1.3** The tool reuses `ExecutionBackend.run_shell` without
  modification. Local and SSH dispatch both work.
- **1.4** No new narrow wrappers (no `git_pull`, `git_fetch`,
  `npm_install`). If someone wants them, they call
  `project_shell(project, "git pull")`.

### U2. Every invocation is visible

As an operator, I want every `project_shell` invocation to
surface as a user-visible event (in addition to the
forensic-grade audit entry) so I can see the sequence of
commands as they run — particularly for `trust_session` flows
where approval doesn't re-prompt.

**Acceptance:**

- **2.1** Every successful `project_shell` invocation emits
  a `tool_executed` event to the Phase 4.5 event log with
  `meta.tool="project_shell"`, `meta.command`, `meta.exit_code`,
  `meta.duration_ms`. Failures (non-zero exit, timeout) emit
  the same event kind with the failure's metadata.
- **2.2** The event's `body` contains stdout + stderr (subject
  to the Phase 4.5 event-push cap so a 100KB output doesn't
  become a 100KB Telegram message — use `telegram_body_cap`).
- **2.3** The audit-log entry (HMAC-chained) continues to
  capture the call unchanged. No duplication of responsibility
  between the two logs — audit is the forensic trail, events
  are the user-facing mirror.
- **2.4** The approval prompt's `args_summary` displays the
  full `command` string up to a ceiling of 1000 chars (the
  default cap of 200 is bypassed for `project_shell`).
  Commands longer than 1000 chars are truncated and flagged in
  the summary as "(truncated; …)" — a 10KB shell command is a
  prompt-injection smell and deserves attention, not silent
  acceptance.

### U3. Deny list extensions are audited and tested

As a reviewer, I want the set of deny patterns added in this
phase to be explicit, justified, and individually tested so
that a future PR adding a pattern lands with a reason and
doesn't accidentally over-match.

**Acceptance:**

- **3.1** Deny-list additions land in
  `gateway/src/gateway/tools/deny_list.py` with a label per
  pattern explaining what it catches.
- **3.2** Each new pattern has a positive test (catches the
  destructive form) and a negative test (doesn't over-match a
  benign command). Tests live in `tests/test_deny_list.py`.
- **3.3** At minimum the phase ships patterns for:
  `rm -rf $FITT_HOME`, `rm -rf $HOME/.fitt`, `git clean -fdx`,
  and the existing Phase 4 list stays in place. Additional
  patterns are added if review surfaces them; the spec does
  not hard-code a final list beyond these.

### U4. Per-client policy defaults match the trust posture

As an operator, I want `project_shell`'s default bucket to
match each client's trust posture so Open WebUI (browser-
exposed) doesn't get shell access just because I added the
tool for the Telegram/IDE flow.

**Acceptance:**

- **4.1** Default bucket for `project_shell`:
  - CLI: `ask`
  - Telegram: `ask`
  - IDE: `ask` (operator can opt into `trust_session` via
    per-client override if they want Continue-style flow)
  - Open WebUI: `block`
- **4.2** The defaults are applied unconditionally — adding
  `project_shell` to the registry doesn't change buckets for
  other tools.
- **4.3** Per-client overrides (via `tools.per_client` in
  `config.yaml`) continue to work for `project_shell` the same
  way they work for other tools. Tests pin this.

### U5. Windows hubs fail loud on missing `bash`

As an operator on a Windows hub, I want the gateway to fail at
boot if `bash -lc` won't work so I don't discover the issue on
the first shell-requiring request.

**Acceptance:**

- **5.1** On gateway startup, a shell-interpreter probe runs
  and records the chosen interpreter (native `bash`, Git Bash,
  WSL) to `app.state`.
- **5.2** If no interpreter is resolvable on the local hub
  (Windows without Git Bash or WSL), a WARNING logs at boot
  and `project_shell`'s local path returns a clear error on
  invocation ("no POSIX shell available; install Git Bash or
  WSL"). The chat path stays up; SSH-based projects still
  work because they use the remote shell.
- **5.3** The SSH dispatch path is unchanged —
  `ExecutionBackend._build_ssh_argv` already wraps the command
  for the remote login shell. The probe does not gate SSH.

### U6. Threat model is documented, not implicit

As a reviewer deciding whether to enable `project_shell` on a
given client, I want the threat model written down in prose
with explicit protected / not-protected lists so I can decide
without reading the code.

**Acceptance:**

- **6.1** `design.md` includes a `## Threat model` section with
  two sub-lists: what v0 protects against (operator mistakes,
  well-known destruction patterns) and what it does NOT
  protect against (compromised model, prompt injection,
  indirect destructive patterns, supply-chain attacks,
  environment poisoning).
- **6.2** The section includes the verbatim one-sentence
  summary:
  > *"Phase 4.7 protects against operator mistakes and
  > well-known destructive patterns; it does not protect
  > against a compromised model or prompt-injection-borne
  > commands at any point past the approval prompt. Do not
  > enable `trust_session` for `project_shell` in sessions
  > whose input channel might carry attacker-controlled
  > content until sandboxing ships."*
- **6.3** `design.md` enumerates non-goals (no allowlist, no
  pattern-based safe-command classifier, no sandbox in v0,
  no interactive commands, no background processes) each
  with a one-sentence rationale pointing at where that
  concern IS addressed (Phase 7+, `bash -lc` mechanics,
  etc.).

### U7. `fitt audit tail` gives the hub operator a live view

As a hub operator, I want a `fitt audit tail` CLI that
live-follows the audit log with a readable formatter so I can
watch tool calls from my terminal without relying on the
Telegram push channel.

**Acceptance:**

- **7.1** A `fitt audit tail -f` command exists that reads new
  audit entries as they're written and prints them with the
  same formatter as the existing `fitt audit tail` (limit
  form). Without `-f` it prints the last N and exits (existing
  behaviour, preserved).
- **7.2** Filters `--tool`, `--session`, `--since` from the
  existing `fitt audit tail` continue to work with `-f`.
- **7.3** Ctrl-C exits cleanly. No daemon, no pidfile — this
  is a terminal-lifetime tailer.

## Definition of done

- `project_shell` tool registered, default bucket `ask`
  everywhere except Open WebUI (`block`).
- Deny-list additions per U3.3 land with positive + negative
  tests.
- `tool_executed` event kind wired; Telegram push formatter
  covers it.
- Windows-hub interpreter probe per U5.
- Threat model + non-goals documented in `design.md`.
- `fitt audit tail -f` shipped per U7.
- `uv run pytest -q` green.
- E2E harness grows a new lifecycle test: approve →
  `project_shell` runs → `tool_executed` event lands →
  rejected path also emits an event.
