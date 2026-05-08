# Phase 5 — Lessons + Decaying History: Requirements

## Context

Phase 2 memory stores today's conversation as flat
markdown. That works fine for short sessions but surfaces
three problems as use widens:

1. **No long-term memory.** A correction from last week is
   gone. Every conversation re-teaches the model
   preferences the user already voiced ("always use uv, not
   pip", "the fitt project uses qwen-coder by default").
2. **Tool-turn poisoning.** Phase 4 persists only the user
   message and the assistant's natural-language reply. The
   tool calls and their results are ephemeral. Observed
   failure mode: SSH was briefly unreachable; the assistant
   wrote "I can't reach SSH" as its final reply; that
   sentence persisted as if it were a factual claim; later
   turns read the stale prose and refused to call tools
   even after SSH was fixed. The strict-xfail e2e test
   `test_session_poisoning_lifecycle` documents this.
3. **History grows without bound.** Yesterday's 50-turn
   session is injected in full today, tomorrow, forever
   (until the flat budget truncates). Older-but-relevant
   turns displace newer-but-boring ones arbitrarily.

Phase 5 addresses all three: a lessons store for learned
corrections; structural tool-turn persistence so paraphrased
refusals can't poison future context; decaying history
injection so days-ago context shrinks over time; a nightly
pruner that drops `YYYY-MM-DD.md` files past the retention
window.

## Scope

Everything lands under the existing `MemoryStore` — no
parallel subsystem, no database, no embeddings. On-disk
format stays plain markdown. The phase is primarily a
format evolution + injection-policy change + one new
identity file (`lessons.md`).

## User stories

### U1. Lessons persist across sessions

As a FITT user, I want to say "always use uv, never pip"
and have the assistant remember that across restarts and
across sessions, so I don't re-teach it daily.

**Acceptance:**

- **1.1** `$FITT_HOME/lessons.md` exists on first run,
  seeded from a template that explains what lessons are
  and how they differ from `preferences.md`.
- **1.2** Every system prompt includes a `[Learned
  corrections]` block rendered from `lessons.md`. The
  block is present even when the file is empty (so the
  model knows the mechanism exists; nothing in the block
  when there are no lessons).
- **1.3** Three inline tools exist: `learn_add(text,
  category?)`, `learn_list()`, `learn_remove(substring)`.
  Default buckets: `learn_add` / `learn_remove` = `ask`;
  `learn_list` = `auto`.
- **1.4** `fitt learn add "..."`, `fitt learn list`,
  `fitt learn remove <substring>` on the CLI.
- **1.5** The file is hand-editable — operators can open
  it in a text editor at any time; the next request picks
  up the change (mtime-based reload, same pattern as
  identity files today).
- **1.6** Ceiling on the `[Learned corrections]` block:
  50 entries by default (configurable via
  `memory.max_lessons`). `learn_add` past the ceiling
  drops the oldest. "Oldest" is the first bullet in the
  file; the tool's append order is the effective age.

### U2. Tool-turn poisoning stops

As a FITT operator, I want a tool-using turn to persist in
a way that later turns read the tool's outcome alongside
the assistant's paraphrase, so a stale refusal can't
misinform future context.

**Acceptance:**

- **2.1** Turns that touched tools persist with at least
  four structured pieces on disk: user message, tool
  calls (name + args summary), tool results (`ok` or a
  short error), final assistant reply.
- **2.2** The on-disk format stays markdown-first; a
  tool-using turn introduces new header types the parser
  understands (e.g. `## <ts> assistant tool_calls` and
  `## <ts> tool <tool_name>`). Unknown headers still
  degrade gracefully per the existing permissive parser.
- **2.3** Loading a tool-using turn produces LLM-shaped
  messages with `role: assistant` carrying `tool_calls`
  and `role: tool` carrying the outcome, matching the
  OpenAI / Anthropic tool-messaging contracts so the
  model's own format isn't surprising.
- **2.4** The strict-xfail e2e test
  `test_session_poisoning_lifecycle` flips green. (That
  test's whole purpose is to fail until this ships.)
- **2.5** Tool results persisted on disk are SHORT. "ok"
  for success; for errors, the first ~300 characters of
  the error text. The full output is NOT persisted — it
  may be large, may be stale tomorrow, and injecting it
  into tomorrow's context would balloon the budget.
- **2.6** Tool *args* persisted on disk are similarly
  short — a few-field summary, not the full JSON. Same
  reasoning as 2.5. A 10KB `content` arg on `write_file`
  doesn't belong in tomorrow's prompt.

### U3. History decays

As a FITT user running daily, I want today's conversation
injected in full but older days to shrink, so the model
has continuity without stuffing the context window.

**Acceptance:**

- **3.1** Memory injection layers by age:
  - Today: full turns, same as Phase 2.
  - Yesterday: first turn + a count marker ("(N more turns
    on 2026-05-07)").
  - 3–30 days ago: one-line marker per day ("2026-04-15:
    4 turns, tools used").
  - 30+ days ago: dropped from context. Files stay on
    disk.
- **3.2** Total history budget capped at 6000 chars
  (configurable via `memory.max_history_chars`). When the
  per-age layers plus the current day exceed the budget,
  oldest layers drop first.
- **3.3** A turn summary line for a tool-using day
  mentions "tools used" so the model knows that day
  wasn't pure chat. Summary text is generated deterministically
  from the turn structure — NOT via a secondary LLM call
  (that would add latency and cost per request).

### U4. Old history files are pruned

As a FITT operator, I want a nightly job that deletes
history files older than the retention window so
`$FITT_HOME/sessions/*/history/` doesn't grow forever.

**Acceptance:**

- **4.1** A background task in the gateway runs daily
  (same pattern as Phase 4.5's event pruner) and deletes
  `history/YYYY-MM-DD.md` files older than
  `memory.history_max_days` (default 90, configurable).
- **4.2** The pruner emits a `system_pruned` event (same
  kind as the event pruner; meta distinguishes via
  `target: "history"`) so `fitt inbox` shows the prune
  happened.
- **4.3** Unit tests cover: files within window kept,
  outside window removed, configurable retention.

### U5. Hand-editing stays honoured

As an operator, I want to edit `lessons.md` or today's
history file directly and have the next request reflect
my changes, so the "shareable by construction" principle
holds.

**Acceptance:**

- **5.1** No in-process cache for lessons. Each request
  re-reads `lessons.md` from disk.
- **5.2** Same for history (already true in Phase 2).
- **5.3** If a history file is hand-edited into an
  unparseable state, memory loading degrades to "what
  could be parsed" and logs a warning, never raises.

### U6. The lessons file is distinguishable from preferences

As a maintainer, I want `lessons.md` (auto-mutated by the
`learn_*` tools) to be conceptually and UX-distinguishable
from `user.md` / `soul.md` (operator-authored identity),
so a model can reason about which source to trust and an
operator knows what's safe to hand-edit.

**Acceptance:**

- **6.1** `lessons.md` lives alongside identity files
  (`$FITT_HOME/identity/lessons.md`), not in a separate
  tree. Same directory, different purpose.
- **6.2** The template for `lessons.md` explains in
  prose that this file is auto-mutated by
  `learn_add`/`learn_remove` and that manual edits are
  OK but may be overwritten by the agent on later
  corrections.
- **6.3** The system-prompt block name is `[Learned
  corrections]`, distinct from `[Capabilities]` and the
  other identity sections, so the model reads it as a
  separate category.

## Definition of done

- All six user stories' acceptance criteria green.
- `uv run pytest -q` passes.
- The strict-xfail session-poisoning test flips green
  and gets un-marked (U2.4).
- No regression in the `fitt session show` CLI output
  for pre-Phase-5 history files (back-compat).
- E2E harness grows one new lifecycle test
  (`test_lessons_lifecycle.py`) covering the `learn_add`
  → next-request-sees-the-lesson loop.
- Roadmap pointer for Phase 5 flipped to DONE with
  validation date.

## Non-goals (deferred to Phase 7+)

- **Vector embeddings / semantic retrieval.** Markdown
  + decay is enough until it demonstrably isn't.
- **Automatic lesson extraction.** The agent calls
  `learn_add` when the user says "remember X." No
  background LLM-driven "infer what lessons I should
  record from recent messages" — too much surprise for
  too little benefit in a single-user setup.
- **Cross-session memory bleed.** Each session's history
  stays its own. Identity + lessons are shared; session
  history is not. A future phase could add
  `fitt session merge` or similar; not now.
- **Full-text search across history.** `grep -r
  $FITT_HOME/sessions/` is the operator answer for v0.
- **Rewriting `user.md` / `preferences.md` from recent
  conversation.** Same reasoning as automatic lesson
  extraction.
- **Importing lessons between users.** Single-user
  tooling.

## Risk / size note

The roadmap's inline draft scoped Phase 5 as "~1
weekend." Writing it up, the honest answer is closer to
"1–2 weekends" — tool-turn persistence (U2) alone is a
disk-format evolution that touches the parser, the
injection path, and every test that seeds history.
Better to name this in the spec than to surprise
ourselves with schedule slip. The four thematic groups
(lessons, tool-turn persistence, decay, pruning) are
individually small but bundle to a meaningful chunk.
