# Phase 5 — Lessons + Decaying History: Requirements

## Background

Through Phase 4.5, FITT can hold tools, schedule work, and push
notifications. It also has Phase 2 memory: identity files always
injected, today's session history always injected.

What's still missing:

- **Learned patterns don't stick.** When you correct the agent —
  "use rg not grep," "this project's tests run with `just test`" —
  the correction applies to the current turn and then evaporates.
  Tomorrow, you'll correct the same thing again.
- **History after today is invisible.** Yesterday's session log
  is on disk but never loaded. "Did I mention this last week?"
  can't be answered.

Phase 5 addresses both with plain markdown primitives:

- **Lessons** — an explicit corrections store with `learn_add` /
  `learn_list` / `learn_remove` tools. Written by the agent when
  you say "remember," edited by hand at any time, injected into
  every system prompt.
- **Decaying history** — when building context, older days get
  compressed to markers. Today full, yesterday truncated, 3-30
  days as one-line markers, 30+ days dropped from prompt (files
  stay on disk).

Both are deliberately non-embedding. No vectors, no SQLite, no
embedding model. Just markdown files read at request time.

## Why now

Two use cases from earlier discussions depend on lessons working:

1. **Monitoring a recurring thing.** "Monitor the RL training"
   the first time requires you to explain the setup. With a
   lesson extracted ("RL training status lives at path X, format
   is Y, create a silent 60s cron"), the second time you can
   just say "monitor training pid 456" and it works.
2. **Repeat corrections.** "Use tabs not spaces in this project."
   Without lessons, you say it every session. With lessons, you
   say it once; `learn_add` captures it; every future session
   sees it in the system prompt.

The decaying history piece is related but smaller. Without it,
every day's history grows into an unbounded prompt cost. With it,
"did I talk about this last week?" becomes answerable without
killing the context budget.

## Goals

1. **Lessons store** at `$FITT_HOME/lessons.md`. Plain markdown,
   bullets or short paragraphs. Hand-editable.
2. **`learn_add`, `learn_list`, `learn_remove` tools** for the
   agent to use.
3. **Injection** of lessons into every session's system prompt as
   a `[Learned corrections]` block.
4. **Cap at ~50 entries**; oldest-pruned when full. Documented,
   not surprising.
5. **Decaying history injection** replacing the Phase 2 "today
   only" logic. Today full; yesterday truncated; older days as
   markers; 30+ days dropped.
6. **History pruning** deletes on-disk files older than
   `memory.history_max_days` (default 90).
7. **`fitt learn` CLI** for direct add / list / remove from the
   terminal.

## Non-goals

- **Vector memory / embeddings.** Deferred. Markdown recall is
  enough until it isn't.
- **Preferences / projects consolidation** (LLM rewrites
  identity files from recent sessions). Deferred. You edit
  identity by hand.
- **Episodic memory.** Deferred.
- **Lesson categories as a hard schema.** Optional free-text
  category field; no enum. Keep it simple.
- **Confidence scores.** Deferred. Every lesson is user-explicit.
- **Dedup by semantic similarity.** Simple substring dedup only.
- **Automatic lesson extraction from conversation patterns.**
  Deferred. Only the agent's explicit `learn_add` call writes
  lessons.

## User stories

### U1 — Explicit learning

> As a user, I want to say "always use `rg` instead of `grep` in
> commands" and have FITT remember that across sessions forever.

Acceptance:
- Agent recognises the pattern ("always", "remember", "never")
  and calls `learn_add(text="use rg instead of grep", category="tool")`.
- `learn_add` is in the `auto` bucket (low-risk, user-explicit).
- The entry appears in `$FITT_HOME/lessons.md`.
- The next session sees it in the system prompt as a
  `[Learned corrections]` bullet.
- Works across the Telegram / IDE / CLI clients equally.

### U2 — Recall across sessions

> As a user, I teach FITT how to monitor RL training once. Next
> week, I just say "monitor training pid 456" and it remembers
> the pattern.

Acceptance:
- After turn 1 (walkthrough + `learn_add`), the lesson is in
  the prompt: "to monitor an RL training, create a silent 60s
  cron that reads status at path ... and send_message on
  state-change."
- In a new session, user says "monitor training pid 456." The
  agent has the lesson available; it substitutes 456 into the
  path and calls `cron_add` directly.

### U3 — Edit by hand

> As a user, I want to open `lessons.md` in a text editor and
> rewrite a lesson because the agent recorded it awkwardly.

Acceptance:
- The file is plain markdown with a simple format (one lesson
  per bullet; optional category tag).
- Changes take effect on the next request (or within N seconds
  via the watcher).
- File corruption (e.g. missing bullets) is handled gracefully;
  invalid lines logged and skipped.

### U4 — CLI management

> As a user, I want `fitt learn list` in a terminal to see
> everything FITT has learned, and `fitt learn remove "grep"`
> to forget by substring match.

Acceptance:
- `fitt learn list` prints lessons with index numbers.
- `fitt learn add "..."` appends a new lesson.
- `fitt learn remove <substring>` removes matching lessons
  (with a prompt if more than one matches).

### U5 — Natural history decay

> As a user, when I ask "did I talk about X last week," the agent
> has enough context to answer (from the decaying injection or
> by reading the file directly).

Acceptance:
- System prompt includes today's history in full (as before).
- Yesterday's history gets injected as "Yesterday: <first entry>
  ... (N total entries)."
- Days 3-30: injected as "YYYY-MM-DD: <entry count> entries."
- Days 30+: not injected. But agent can call `read_file` on a
  specific dated history file if asked.
- Total history section capped at ~6000 chars (configurable).

### U6 — History pruning

> As a user, I don't want my session history directory to grow
> forever.

Acceptance:
- A built-in cron (not user-visible) runs nightly and deletes
  `history/YYYY-MM-DD.md` files older than
  `memory.history_max_days` (default 90).
- Pruning emits a system event ("pruned N history files").

### U7 — Lessons cap

> As a user, I expect the `[Learned corrections]` block to stay
> at a reasonable size even after months of use.

Acceptance:
- `lessons.md` is capped at `memory.lessons_max_entries`
  (default 50). When the agent calls `learn_add` and the file
  is at the cap, the oldest entry is removed.
- `fitt learn list` shows the total count and a warning when
  approaching the cap.
- User can raise the cap in `config.yaml` if they want more.

## Scope boundaries

**In scope:**

- `$FITT_HOME/lessons.md` format, reader, writer.
- `learn_add`, `learn_list`, `learn_remove` inline tools.
- Injection of lessons into the system prompt.
- Decaying history reader.
- History pruning cron.
- `fitt learn` CLI.
- Config additions to `memory:` section.

**Out of scope:**

- Vector / semantic memory.
- Preferences / projects consolidation.
- Cross-session episodic memory.
- Automatic lesson extraction from non-explicit corrections.
- Lessons per project (all lessons are global for v0).

## Risks and open questions

### R1 — Agent writes noisy lessons

**Risk:** Agent interprets every request as a "remember this"
and fills lessons.md with junk.

**Decision:** system prompt explicitly guides the agent to only
call `learn_add` when the user uses words like "remember," "always,"
"never," "prefer," or after a direct correction ("no, use X
instead"). Reviewed via eval harness on sample prompts. If noise
becomes a problem, tighten the prompt.

### R2 — Lessons conflict

**Risk:** "Always use rg" today; "Always use ripgrep" next week.
Two similar entries in the file.

**Decision:** substring dedup on write. If the new entry contains
or is contained in an existing entry (case-insensitive), replace
the older one with the newer. Not perfect but avoids obvious
duplicates. The user can always hand-edit.

### R3 — Large lessons eat context

**Risk:** Agent writes a 2000-word "lesson" that's actually a
transcript.

**Decision:** per-entry length cap (default 500 chars). Truncate
on write with an ellipsis. User can override via direct edit.

### R4 — Decaying history still blows the budget

**Risk:** Even with decay, long sessions produce enough "today"
content to push total prompt past the model's context window.

**Decision:** today's history also has a cap (default 6000 chars).
When exceeded, oldest turns get dropped from the injection (file
stays full on disk). The user sees a "(N older turns truncated)"
marker.

### R5 — File watch performance

**Risk:** Watching `lessons.md` for hand-edits adds complexity;
watchfiles or inotify may be flaky inside the Docker container.

**Decision:** don't watch. Read lessons.md on every request. It's
small (<10KB typically). Cheap. One less moving part.

### R6 — Loss on corruption

**Risk:** A malformed write (partial, interrupted) truncates
lessons.md.

**Decision:** atomic writes only (tmp + rename). Every write goes
through a helper that writes to a tmp file and renames. Matches
Phase 4's approach for `projects.yaml`.

### R7 — What about lessons for a specific project?

**Risk:** "Use tabs in retro-ai but spaces in home-ai-cluster."
Global lessons can't express this.

**Decision:** v0 accepts only global lessons. If a lesson needs
to scope to a project, the user says so in the lesson text
itself ("in retro-ai, use tabs"). Project-scoped lessons are a
later addition if needed.

## Success criteria

Phase 5 is done when:

1. The RL-monitoring scenario works with recall: first time
   requires explanation; next time, "monitor training pid 456"
   with no other context triggers the right cron.
2. A correction ("use rg not grep") given once is respected in a
   completely new session.
3. `fitt learn list / add / remove` CLIs work.
4. `lessons.md` stays under the cap over a week of use.
5. `read_recent_history` injection fits in the documented
   character budget across days.
6. History pruning removes files older than 90 days on the
   nightly cron.
7. All existing tests pass.
8. Author has lived with Phase 5 for 1 week without wanting to
   revert.
