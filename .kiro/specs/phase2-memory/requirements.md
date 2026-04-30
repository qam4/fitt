# Requirements: FITT Phase 2 — Memory v0

## Overview

Phase 1 gave FITT a gateway. Phase 2 gives it a memory. The gateway
remembers *who the user is* (identity) and *what they said today*
(conversation history) across gateway restarts. The "goldfish mode"
is gone: restart the process, come back in an hour, and the
conversation continues from where it left off.

Memory is plain markdown on disk. No vector database, no Mem0, no
external service. This keeps the data transparent (readable and
editable in any text editor), portable (no proprietary format), and
debuggable (when the AI says something weird, you can open the file
and see exactly what it was fed).

Phase 2 is session-aware from the start even though only a single
`main` session exists in v0. This prevents a painful migration when
Phase 2.5 adds named sessions; the memory layer already operates on
a "which session?" primitive.

## User Stories

### 1. Identity injection

As a user, I want FITT to know who I am and what I'm working on so
I don't re-introduce myself at the start of every conversation.

#### Acceptance Criteria
- 1.1 Three identity files live under `~/.fitt/identity/`:
  - `user.md` - facts about me (name, role, projects, preferences).
  - `soul.md` - FITT's personality and response style.
  - `tools.md` - summary of tools FITT has access to (manually
    maintained in Phase 2; auto-generated in Phase 4).
- 1.2 On first gateway startup, any missing identity file is created
  from a default template.
- 1.3 On every `POST /v1/chat/completions`, all three files are read
  and prepended to the system message sent to the upstream LLM.
- 1.4 Edits to identity files take effect on the next request (no
  gateway restart required).

### 2. Today's history persistence

As a user, I want the gateway to remember the current day's
conversation across process restarts so a crash or reboot doesn't
lose context.

#### Acceptance Criteria
- 2.1 Each conversation turn is appended to
  `~/.fitt/sessions/<id>/history/YYYY-MM-DD.md` in a human-readable
  format, where `<id>` is the active session (default: `main`).
- 2.2 A turn entry has two blocks: the user message and the
  assistant's reply, each with an ISO-8601 timestamp.
- 2.3 Before dispatching a new request to the upstream LLM, the
  gateway loads today's history file for the active session and
  appends each turn as an additional message (alternating user /
  assistant) before the new user message.
- 2.4 If no history file exists for today, the request proceeds
  with no history (empty additional messages).
- 2.5 The gateway creates the session directory and parent
  directories on demand if missing.
- 2.6 History writes happen after the response is complete, not
  during streaming. A partial response that errors out is not
  persisted.

### 3. The "keys on the counter" test

As a user, I want to verify that memory actually works end to end.

#### Acceptance Criteria
- 3.1 Given a fresh gateway and an empty history file for today,
  when the user sends "remember: my keys are on the counter" and
  the gateway responds, and then the gateway process is restarted,
  and the user sends "where are my keys?", the response mentions
  "counter" (case-insensitive).
- 3.2 The test is automated in the integration test suite and uses
  a mocked upstream LLM so it doesn't depend on network or token
  quotas.

### 4. Session scoping (v0: main only)

As a user, I want the memory layer to be ready to support multiple
named sessions even though v0 only exposes `main`.

#### Acceptance Criteria
- 4.1 Every request is associated with a session id. In v0, the
  only supported id is `main`; any other value is rejected with
  HTTP 400.
- 4.2 The session id is determined in this order:
  1. `X-FITT-Session` request header if present.
  2. Default to `main`.
- 4.3 The history file path always includes the session id
  (`sessions/<id>/history/YYYY-MM-DD.md`), never a legacy flat
  location.
- 4.4 Phase 2 does not expose a `fitt session` CLI. That's Phase
  2.5's job.

### 5. Memory context budget

As a user, I want the gateway to avoid exceeding the model's
context window because of an overly long history file.

#### Acceptance Criteria
- 5.1 A configurable `memory.max_history_chars` limit (default:
  24000 characters, roughly 6000 tokens) is enforced on the
  history slice loaded into the prompt.
- 5.2 If today's history exceeds the limit, only the most recent
  turns that fit are loaded; older turns are silently dropped from
  the prompt. The file on disk is NOT truncated.
- 5.3 A gateway log event at `info` level reports when truncation
  happens, including the number of bytes dropped.

### 6. Disable / inspect

As a developer, I want to debug and operate memory without
rebuilding the gateway.

#### Acceptance Criteria
- 6.1 A `memory.enabled` config flag (default: `true`) disables
  memory injection entirely when set to `false`. The gateway
  behaves as in Phase 1.
- 6.2 A `fitt memory show` CLI command prints the currently-loaded
  context (identity + today's history) for a given session to
  stdout, formatted as the LLM would see it.
- 6.3 A `fitt memory append` CLI command appends a
  `user`/`assistant` pair manually (useful for seeding context or
  testing).

## Non-Goals (explicit)

- **No cross-day history (yet).** "Yesterday" loading, decay,
  summarization, and compaction are Phase 7's concerns. v0 is
  "today only."
- **No vector search, no embeddings, no Mem0.** Plain markdown is
  the source of truth in v0.
- **No per-session identity.** Identity is shared across all
  sessions (one user, one FITT persona). Phase 2.5's named
  sessions still read the same `identity/` directory.
- **No automatic summarization.** Truncation by oldest-first is
  the only context-budget strategy in v0.
- **No multi-user.** Single user, single identity.
- **No Telegram or IDE-specific session tracking.** That's Phase
  2.5 and Phase 3's problem.

## Shareable-by-construction

Same invariants as Phase 1:

- The repo ships no personal identity. Users create their own
  `user.md` from the template the gateway generates.
- Default templates include `TODO` placeholders so users have to
  fill them in to see full value.
- History files are gitignored by `.gitignore` (they live in
  `~/.fitt/`, which is outside the repo, but the rule is documented
  so anyone vendoring FITT differently doesn't accidentally commit
  logs).
