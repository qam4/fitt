# Phase 5 — Lessons + Decaying History: Design

## Overview

Four pieces, each small, composed on top of the existing
Phase 2 `MemoryStore`. Nothing moves to a new directory;
nothing touches the database-adjacent primitives. The
disk format stays markdown-first.

```
$FITT_HOME/
├── identity/
│   ├── user.md                   (operator-authored, unchanged)
│   ├── soul.md                   (operator-authored, unchanged)
│   ├── tools.md                  (operator-authored, unchanged)
│   └── lessons.md                NEW — agent/operator-mutated
└── sessions/
    └── main/
        └── history/
            ├── 2026-05-08.md     existing shape for chat turns;
            │                     tool-using turns add new headers
            └── 2026-04-08.md     older days; pruned past retention
```

Four principles.

1. **Markdown stays the source of truth.** Every piece —
   lessons, identity, history, decayed summaries — is text
   a human can read in a text editor. The gateway renders
   the LLM-shape at request time from the text; the LLM
   never writes directly to a database.
2. **Parser stays permissive.** The Phase 2 parser ignores
   unknown header lines. New tool-using headers slot in
   without breaking back-compat: a pre-Phase-5 history
   file still loads in a post-Phase-5 gateway.
3. **Decay policy is layered, not LLM-driven.** Summaries
   of older days are computed deterministically from turn
   structure (count, "tools used" flag) — no secondary LLM
   call per request. Cost + latency stay bounded.
4. **Lessons live alongside identity.** One directory, one
   mental model: "things that go into every system
   prompt." Lessons are distinguishable in prose (template
   explains the auto-mutation contract) but structurally
   they're just another file the store reads on boot.

## Architecture

```
                       request comes in
                              │
                              ▼
                    MemoryStore.load_context(session)
                              │
            ┌─────────────────┼──────────────────┐
            ▼                 ▼                  ▼
     _load_identity     _load_lessons     _load_decaying_history
     (unchanged)        NEW              NEW (replaces flat load)
            │                 │                  │
            └────────┬────────┴────────┬─────────┘
                     ▼                 ▼
           system_prefix          history_messages
            (rendered)              (OpenAI-shape list,
                                     with tool roles for
                                     tool-using turns)

                       periodically:
                              │
                              ▼
                    HistoryPruner.tick()
                    (drops YYYY-MM-DD.md past retention;
                     emits system_pruned event)
```

## On-disk format evolution

### `identity/lessons.md` (new file)

```markdown
# Learned corrections

This file is auto-mutated by the `learn_add` / `learn_remove`
tools and by `fitt learn`. You can hand-edit it at any
time — the next request picks up your changes. Manual
edits may be overwritten later if the agent records a
conflicting correction.

Lessons are hints carried into every system prompt as the
`[Learned corrections]` block. Keep each entry short.
Two-sentence paragraphs work; long prose stops being
useful at scale.

## Active lessons

- (empty)
```

The "Active lessons" section is what the tools edit. Entries
are bullets; optional `[category]` prefix for operator
organisation (`[tooling]`, `[style]`, `[preferences]`);
agent-added entries skip the category.

### `sessions/<s>/history/YYYY-MM-DD.md` (evolved format)

**Today (Phase 2/4) — chat-only turn:**

```markdown
## 2026-05-08T14:02:11Z user

what's the git status?

## 2026-05-08T14:02:14Z assistant

Your working tree is clean.
```

**New (Phase 5) — tool-using turn:**

```markdown
## 2026-05-08T14:05:03Z user

run ls in home-ai-cluster

## 2026-05-08T14:05:06Z assistant tool_calls

- project_shell(project='home-ai-cluster', command='ls -la | head -n 5')

## 2026-05-08T14:05:08Z tool project_shell

ok

## 2026-05-08T14:05:11Z assistant

Here are the first five files…
```

**Rules:**

- A turn with tool calls gets up to three blocks
  instead of two: `assistant tool_calls`, one `tool <name>`
  per call in order, final `assistant` (the natural-language
  reply).
- `tool <name>` body is SHORT: `ok` or `exit=N: <first 300
  chars of stderr>`. Never the full output — belongs in the
  tool-result message during the live turn, not in
  tomorrow's context.
- `assistant tool_calls` body is a bullet list. Each bullet
  is `<tool-name>(<first 80 chars of args-summary>)`.
  Deterministic, compact, greppable.

**Parser rules:**

- Extended `_HEADER_RE` to match `user` / `assistant` /
  `assistant tool_calls` / `tool <name>` / `system`.
- Pre-Phase-5 files (with only user/assistant headers)
  load identically to today (back-compat).
- A post-Phase-5 file read by a future version with more
  header kinds degrades gracefully — unknown headers are
  dropped with a debug log.

### Loading tool-using turns

When a `tool_calls` turn is loaded, it produces this message
sequence (LLM-shape):

```python
[
    {"role": "user", "content": "run ls in home-ai-cluster"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "persisted-<hash-of-args>",
                "type": "function",
                "function": {
                    "name": "project_shell",
                    "arguments": '{"project": "...", "command": "..."}'
                }
            }
        ],
    },
    {"role": "tool", "tool_call_id": "persisted-<hash>", "content": "ok"},
    {"role": "assistant", "content": "Here are the first five files…"},
]
```

`tool_call_id` is derived deterministically from the args
bullet text so the tool role correctly pairs with its
assistant-tool_calls entry. The model doesn't see the id
as significant; it only matters that assistant and tool
messages share a valid id.

## Decaying injection

`_load_and_truncate_history` becomes
`_load_decaying_history`:

```python
def _load_decaying_history(
    self, session: str, now: date
) -> tuple[list[dict], int]:
    """Return a mixed list of:
    - today's turns in full (OpenAI message shape)
    - yesterday's first turn + a count marker
    - days 3-30: one-line summary per day
    - day 30+: dropped
    Truncate oldest layer first if total > max_chars.
    """
```

Helper for the 3-30 range:

```python
def _summarise_day(path: Path) -> str:
    turns = _parse_turns(path.read_text())
    n = sum(1 for t in turns if t.role == "user")
    tools_used = any(t.role.startswith("assistant tool") for t in turns)
    marker = "with tools" if tools_used else "chat only"
    day = path.stem  # "YYYY-MM-DD"
    return f"{day}: {n} user turns ({marker})"
```

This becomes an injected `system` message in history:

```python
{"role": "system", "content": "[Past activity]\n\n" + "\n".join(markers)}
```

Or folded into the system_prefix. Prefer injecting as a
single system message before the day's turns so the model
sees "here's where we've been" explicitly. Decision pinned
in the spec's Open-design-decisions section.

## Tool-turn persistence — the critical path

Today's `MemoryStore.append_turn(session, user, assistant)`
assumes chat-only turns. Phase 5 introduces:

```python
@dataclass(frozen=True, slots=True)
class PersistedToolCall:
    tool_name: str
    args_summary: str       # ~80 chars, deterministic
    result_status: str      # "ok" or "exit=N"
    result_summary: str     # first ~300 chars

def append_turn(
    self,
    session_id: str,
    user_message: str,
    assistant_message: str,
    *,
    tool_calls: list[PersistedToolCall] | None = None,
) -> None:
    ...
```

Default behaviour unchanged when `tool_calls=None` (the
common path — plain chat turn). When `tool_calls` is
present, the turn block expands to include the three extra
sub-blocks.

Callers that need to emit `tool_calls`:

- **`chat.py`** — the tool loop collects each call's
  `PersistedToolCall` and hands them to `append_turn`
  after the loop resolves.
- **`cron_runner.py`** — same pattern.
- **`detach.py`** — the detached worker finishes the loop
  and calls `append_turn` with the accumulated calls.

The agent-loop-internal data structure for a call is
already most of `PersistedToolCall`; we just need to
summarise `args` via a small helper (reuse the logic in
`approval._summarise_args` if we can — but widen for
project_shell commands the same way the approval prompt
does).

## `learn_*` inline tools

```python
# gateway/src/gateway/tools/lessons.py

async def _tool_learn_add(args: dict, ctx: ToolContext) -> ToolResult:
    text = args.get("text")
    category = args.get("category") or ""
    if not isinstance(text, str) or not text.strip():
        return ToolResult.error("'text' required and non-empty")
    lessons = ctx.lessons    # new ToolContext field
    if lessons is None:
        return ToolResult.error("lessons store not wired (gateway bug)")
    lessons.add(text.strip(), category=category.strip() or None)
    return ToolResult.ok(f"remembered: {text.strip()[:80]}")
```

Matching `_tool_learn_list` (reads bullets) and
`_tool_learn_remove` (substring match, returns count
removed). All three use a new `LessonsStore` class:

```python
class LessonsStore:
    def __init__(self, path: Path, *, max_entries: int = 50) -> None: ...
    def read(self) -> list[Lesson]: ...
    def add(self, text: str, *, category: str | None = None) -> None: ...
    def remove(self, substring: str) -> int: ...
    def render_block(self) -> str: ...  # [Learned corrections] block
```

File operations are write-through (read, mutate, write)
with fcntl-style locking matching `CronService`. The file
is small enough that rewriting on every mutation is fine.

## `fitt learn` CLI

Mirrors `fitt cron`:

- `fitt learn list` — pretty-prints active lessons.
- `fitt learn add "text" [--category tooling]` — add one.
- `fitt learn remove <substring>` — remove matches;
  prints N removed.
- `fitt learn path` — print the on-disk path so operators
  can `$EDITOR` it directly.

Bypasses approval middleware (CLI operator === human,
same posture as `fitt cron add`).

## History pruner

Same shape as Phase 4.5's `EventPruner`. Lives at
`gateway/src/gateway/history_pruner.py`:

```python
class HistoryPruner:
    def __init__(
        self,
        *,
        memory: MemoryStore,
        events: EventLog,
        max_age_days: int,
        poll_interval_secs: float = _DEFAULT_POLL_INTERVAL_SECS,
        prune_interval_secs: float = _DEFAULT_PRUNE_INTERVAL_SECS,
        anchor_path: Path | None = None,
    ) -> None: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def tick(self, *, now: float | None = None) -> int | None: ...
```

`tick` walks `$FITT_HOME/sessions/*/history/*.md`,
computes each file's date from the filename, drops past
retention. Emits `system_pruned` with
`meta.target="history"` and `meta.removed=<count>` so
`fitt inbox` sees it alongside the event-pruner's
entries.

## Configuration additions

```yaml
memory:
  # Existing fields kept.
  enabled: true
  max_history_chars: 6000      # existing; cap tightened from 24000
  identity_dir: ~/.fitt/identity
  sessions_dir: ~/.fitt/sessions

  # Phase 5 additions.
  max_lessons: 50               # ceiling on [Learned corrections]
  history_max_days: 90          # prune history older than this
  history_decay:                # (optional block; defaults below)
    today: full                 # full|first-turn|summary
    yesterday: first-turn-plus-count
    recent_window_days: 30      # days 3..N rendered as one-line summaries
```

All Phase 5 fields have sensible defaults; operators can
omit the block entirely.

## Testing

### Unit

- `test_lessons_store.py`: add, list, remove, max_entries
  rollover, file persistence, mtime-based reload, category
  parsing.
- `test_tools_lessons.py`: each of the three tools in
  isolation.
- `test_memory_decay.py`: today-full, yesterday-first +
  count, 3-30 one-line summaries, 30+ dropped, budget
  truncation prefers dropping oldest layer.
- `test_memory_tool_turns.py`: round-trip append + load
  of a tool-using turn produces the expected OpenAI
  message shape; parser handles pre-Phase-5 chat-only
  files (back-compat); unknown headers degrade gracefully.
- `test_history_pruner.py`: retention boundary, multiple
  sessions, event emission, anchor persistence (restart
  resume), empty tree is no-op.

### E2E

- `tests/e2e/test_lessons_lifecycle.py`: stubbed LLM
  emits `learn_add("always use uv")` → approver approves
  → next chat request injects the `[Learned corrections]`
  block containing the new lesson. Asserts round-trip via
  the HTTP surface.
- **Flip**: `tests/e2e/test_session_poisoning_lifecycle.py`
  un-xfails. Assertion stays the same ("stale refusal is
  NOT verbatim in next dispatch") — it passes now because
  the tool-result "ok" or short-error replaces the
  verbatim NL refusal.

### Back-compat smoke

- Load a Phase-2-shape history file in the Phase-5 parser.
  Expected: same messages out, no warnings.
- Load a Phase-5-shape file in a hypothetical-future-version
  parser that doesn't know `tool_calls` headers. Expected:
  drops unknown blocks, preserves user + assistant turns,
  logs debug.

## Rollout

Sub-phase order (each commits independently):

1. `LessonsStore` + unit tests. No wiring yet.
2. Lessons system-prompt injection (append a `[Learned
   corrections]` block to `system_prefix`). Capability
   block + lessons + identity in one rendered prefix.
3. `learn_*` inline tools + unit tests.
4. `fitt learn` CLI + tests.
5. Tool-turn persistence format: parser extensions +
   `append_turn` signature grows `tool_calls=...` +
   unit tests for round-trip.
6. Rewire `chat.py` / `cron_runner.py` / `detach.py` to
   collect `PersistedToolCall` and pass to `append_turn`.
7. Decaying injection: replace `_load_and_truncate_history`
   with `_load_decaying_history` + unit tests.
8. `HistoryPruner` + unit tests + wire at create_app.
9. E2E lifecycle test for lessons.
10. Flip `test_session_poisoning_lifecycle` off xfail;
    confirm green.
11. Live validation on the NAS.

Steps 1–4 can ship without steps 5–7 if we want a smaller
first commit; steps 5–10 are where the real risk lives
(format change + injection-path rewire + test suite
migration).

## Open design decisions

1. **Decay summaries: prepend to first-turn of the day,
   or inject as a separate system message?** A separate
   `system` message at the top of history keeps the
   chat-turns list a clean conversation log. That's my
   lean. Revisit if model behaviour suggests the
   summaries get ignored when siloed in a system message.

2. **Tool-turn persistence failure mode.** If
   serialising a `PersistedToolCall` raises (disk full,
   permission denied), do we drop the whole turn or
   persist the chat-only shape? v0 persists chat-only
   with a warning — "better something than nothing." A
   future hardening pass could add a retry queue.

3. **`learn_add` without `ctx.client == "telegram"`.**
   Approval on the CLI bypasses the middleware; from
   Open WebUI, the default bucket (`ask`) fires through
   Telegram. That's the same posture as every other
   mutating tool — no special-case needed.

4. **Maximum entry length.** A single lesson of 500
   chars is fine; 5000 is a smell. Cap at 1000 chars
   per entry with a truncation warning?  v0: no cap
   (trust the human/agent). If the file ever ends up
   with a 10KB entry we'll add one.

5. **Cross-session lesson visibility.** v0: every
   session reads the same `lessons.md`. That's the
   right default for a single-user system. A future
   phase could add per-session `lessons-<s>.md` if two
   distinct workflows collide.

6. **Do we need a separate `preferences.md` rewrite
   mechanism?** The roadmap's Phase 7+ mentions
   "automatic preferences/projects consolidation (LLM
   rewrite of `preferences.md` from recent messages)."
   v0 explicitly doesn't do this — `user.md` is
   operator-authored, `lessons.md` is agent-editable.
   Keeping the split clean here is what lets us say
   "manual edits are safe" about `user.md`.

## Correctness properties

- **P1.** A lesson added via `learn_add` is visible in
  the next request's system prompt.
- **P2.** A tool-using turn loaded from disk produces a
  message sequence with a `role: tool` entry between the
  `assistant` with `tool_calls` and the final
  `assistant`.
- **P3.** A Phase-2-shape history file loads identically
  before and after Phase 5 (back-compat).
- **P4.** `learn_add` past `max_lessons` drops the
  oldest.
- **P5.** The pruner drops files older than
  `history_max_days` and only those.
- **P6.** The session-poisoning e2e test flips green
  without modification (its assertion is "stale NL
  refusal NOT in next dispatch" — exactly what the
  tool-outcome-replaces-paraphrase rule produces).

## What this phase explicitly does not build

Enumerated in `requirements.md` (section "Non-goals")
and summarised here so a skimmer sees the boundaries:

- No embeddings, no semantic retrieval.
- No automatic lesson extraction.
- No cross-session memory bleed.
- No full-text search.
- No LLM-driven rewrite of identity files.
- No importing lessons between users.
