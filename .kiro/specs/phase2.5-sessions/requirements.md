# Requirements: FITT Phase 2.5 — Sessions

## Overview

Phase 2 made the gateway session-aware, but v0 only accepted `main`.
Phase 2.5 adds named sessions so users can segment their history into
logical scopes: one session for retro-ai work, another for
home-automation experiments, another for a random one-off, without
any of them contaminating each other.

Sessions are user-controlled and persistent. They're created via the
`fitt session` CLI, stored on disk (listed in a small index file),
and selected on each chat request via the `X-FITT-Session` header.

Identity remains shared across all sessions. Each session has its own
history. Both stay where Phase 2 put them: under `~/.fitt/`.

## User Stories

### 1. List, create, and rename sessions

As a user, I want CLI commands to manage sessions so I can segment
my conversations by topic or project.

#### Acceptance Criteria
- 1.1 `fitt session list` prints every configured session with its
  display name and created timestamp.
- 1.2 `fitt session new <id> [--name "Display Name"]` creates a new
  session with the given id. Ids must match `^[a-z0-9][a-z0-9-]*$`
  (lowercase, digits, hyphens, no leading hyphen). Optional
  human-readable name defaults to the id.
- 1.3 `fitt session rename <id> --name "New Display"` changes a
  session's display name without touching its id or history.
- 1.4 `fitt session archive <id>` marks a session as archived. It
  remains on disk and loadable, but is hidden from `list` by default
  and rejected for new chat requests.
- 1.5 `fitt session unarchive <id>` re-activates a previously
  archived session.
- 1.6 The `main` session is created on first load if missing and
  cannot be deleted, renamed, or archived.

### 2. Session selection in chat requests

As a user, I want to tell the gateway which session a request
belongs to so history is scoped correctly.

#### Acceptance Criteria
- 2.1 The `X-FITT-Session` header names the active session. Absent
  header defaults to `main`.
- 2.2 Any configured session id (active, not archived) is accepted.
- 2.3 An archived or non-existent id returns HTTP 400 with
  `type: unknown_session` and a list of active sessions.
- 2.4 The gateway reads the session index fresh on each request so
  newly-created sessions are available immediately without a
  restart.
- 2.5 The response's `X-FITT-Session` header reflects the session
  actually used.

### 3. Session history isolation

As a user, I want my retro-ai conversation to not leak into my
home-automation conversation.

#### Acceptance Criteria
- 3.1 Two distinct sessions' history files live under distinct
  on-disk directories: `sessions/<id>/history/YYYY-MM-DD.md`.
- 3.2 A request against session A does not load session B's history
  into its context.
- 3.3 A response in session A does not get appended to session B's
  history file.

### 4. Session index

As a user, I want the set of configured sessions to persist across
gateway restarts.

#### Acceptance Criteria
- 4.1 Sessions are stored in `~/.fitt/sessions/sessions.json` with
  entries `{ id, name, created_at, archived }`.
- 4.2 The index is created on first gateway startup with a single
  `main` entry.
- 4.3 Editing the index file directly is supported (not recommended,
  but not forbidden); the gateway reads fresh on each request.
- 4.4 Corrupted JSON is treated as "just `main` exists" with a
  warning log; the gateway does not refuse to start.

### 5. Continue using Phase 2 storage layout

As a developer, I want Phase 2.5 to be purely additive so nothing
gets migrated.

#### Acceptance Criteria
- 5.1 Phase 2 history files (under `sessions/main/history/`)
  continue to work without modification.
- 5.2 The `MemoryStore` class is unchanged in shape; only the
  session-resolution path widens.

## Non-Goals (explicit)

- **No per-interface automatic session routing.** Telegram-from-phone
  and Telegram-in-a-group both default to `main` until Phase 3
  decides its own rule. Phase 2.5 only provides the primitive.
- **No session auto-creation from user-provided ids.** You must
  `fitt session new` first. This prevents typos from forking history
  into new directories.
- **No session-specific identity.** Identity is single-user,
  single-persona.
- **No session quotas or expiry.**
- **No cross-session search.** A future RAG phase may add it.

## Shareable-by-construction

- The sessions index contains no personal data beyond the id and
  display name the user chose. If someone shares FITT, their session
  ids don't leak anything except "the user had a session called
  retro-ai."
- The index file is user-writable; no secrets.
