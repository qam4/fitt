# Phase 5 — Lessons + Decaying History: Design

## Overview

Two small pieces of machinery, both backed by plain markdown files.

- **Lessons** = `$FITT_HOME/lessons.md`, injected into every
  session's system prompt as `[Learned corrections]`.
- **Decaying history** = a replacement for Phase 2's "today-only"
  history loader. Adds compressed injection for yesterday and
  older days. Daily pruning deletes 90+ day old files.

No new databases. No embeddings. No consolidation LLM. Phase 5
ships in a weekend.

## Data model

### `lessons.md` format

```markdown
# Lessons

- use rg instead of grep for repo searches
- always use `docker compose` (v2) not `docker-compose` (v1)
- preferred: 4-space indentation in Python; 2-space in YAML
  `[category: style]`
- monitor RL training jobs by creating a silent 60s cron that
  reads `<path>/<pid>/status.json` and calls send_message when
  state is completed or failed
  `[category: pattern]`
```

Rules:
- Top-level `# Lessons` header optional (ignored by reader).
- Each lesson is one bullet (`- `) at the top level.
- Continuation lines (no bullet, indented) are part of the same
  lesson.
- Optional `[category: xxx]` tag on its own indented line,
  parsed into metadata.
- Comments and blank lines ignored.
- Max entries capped at `memory.lessons_max_entries` (default
  50). Exceeding the cap drops the oldest (first in file).

### In-memory model

```python
@dataclass
class Lesson:
    id: str                       # sha1(text)[:8] for dedup
    text: str                     # the lesson content
    category: str = ""            # free text; optional
    added_ts: float = 0.0         # when added


class LessonStore:
    def load(self) -> list[Lesson]: ...
    def save(self, lessons: list[Lesson]) -> None: ...  # atomic write

    def add(self, text: str, category: str = "") -> Lesson: ...   # substring dedup
    def remove_matching(self, substring: str) -> int: ...
    def list(self) -> list[Lesson]: ...
    def get_context(self, cap_chars: int) -> str: ...   # formatted block
```

Atomic writes: tmp file + rename, same pattern as projects.yaml.

## Lessons injection

The gateway's chat handler, when building the system prompt,
inserts a `[Learned corrections]` block after identity and
capabilities, before the session history.

```
[Identity]
<user.md content>

[Capabilities]
<tools list>

[Learned corrections]
- use rg instead of grep for repo searches
- always use docker compose not docker-compose
- preferred 4-space indent in Python, 2-space in YAML [style]
- monitor RL training jobs: silent 60s cron ... [pattern]

[Session history — today]
<today's full entries>

[Session history — yesterday]
<first entry>
(N total entries yesterday)

[Session history — older]
- 2026-04-28: 12 entries
- 2026-04-27: 8 entries
- 2026-04-26: 3 entries
...
```

Cap: the lessons block is capped at `memory.lessons_block_cap`
chars (default 3000). If the lessons in the file exceed the cap,
oldest-first truncation with a "(N older lessons omitted)" marker.

## Lessons inline tools

Three new tools, registered in `gateway/tools/lessons.py`:

```python
@tool(name="learn_add", bucket=ApprovalBucket.AUTO, ...)
async def learn_add(args, context) -> ToolResult:
    text = args["text"]
    if len(text) > MAX_LESSON_CHARS:
        text = text[: MAX_LESSON_CHARS - 3] + "..."
    lesson = context.lessons.add(text, args.get("category", ""))
    return ToolResult.ok(f"Learned: {lesson.text}")


@tool(name="learn_list", bucket=ApprovalBucket.AUTO, ...)
async def learn_list(args, context) -> ToolResult:
    lessons = context.lessons.list()
    if not lessons:
        return ToolResult.ok("No lessons yet.")
    lines = [f"{i+1}. {L.text}" for i, L in enumerate(lessons)]
    return ToolResult.ok("\n".join(lines))


@tool(name="learn_remove", bucket=ApprovalBucket.ASK, ...)
async def learn_remove(args, context) -> ToolResult:
    n = context.lessons.remove_matching(args["substring"])
    return ToolResult.ok(f"Removed {n} lessons.")
```

`learn_add` and `learn_list` are `auto`. `learn_remove` is `ask`
because it's a destructive operation (rare but possible source
of loss).

## Decaying history reader

New module `gateway/memory_decay.py`:

```python
@dataclass
class HistoryContext:
    today_text: str                 # today's full entries (truncated if large)
    yesterday_text: str              # "Yesterday: <first entry>..."
    markers_text: str                # "- 2026-04-28: 12 entries\n..."


def build_history_context(
    sessions_dir: Path, session_key: str,
    today: date,
    today_cap_chars: int = 24_000,
    yesterday_cap_chars: int = 2_000,
    markers_days: int = 30,
    markers_cap_chars: int = 1_500,
) -> HistoryContext: ...
```

Algorithm:
1. Today: read `<session_dir>/history/<today>.md`, if it exists.
   If it exceeds `today_cap_chars`, drop oldest entries until it
   fits; prepend "(N older turns truncated)" marker.
2. Yesterday: read `<session_dir>/history/<yesterday>.md`. Take
   the first entry + a tail summary ("(N total entries)"). Cap
   at `yesterday_cap_chars`.
3. Markers (day 3 .. markers_days ago): for each date in range,
   if the history file exists, count entries (cheap grep for
   `^##` header). Produce one line per day: `- <date>: <count>
   entries`. Cap the block at `markers_cap_chars`.

Integration: the existing context builder (Phase 2) is replaced
with this. Session registry still applies (today/yesterday/
markers are per-session).

## History pruning

A built-in cron (registered at gateway startup, not
user-visible in `cron.json`):

```python
{
    "id": "system_history_prune",
    "name": "prune-history",
    "schedule": {"kind": "cron", "cron_expr": "0 4 * * *"},  # 04:00 daily
    "message": "# internal; fires a system callback",
    "silent": True,
    "system": True,
}
```

The firing callback (not going through the model) directly calls
`prune_history(max_age_days)`:

```python
def prune_history(sessions_dir: Path, max_age_days: int) -> int:
    cutoff = date.today() - timedelta(days=max_age_days)
    removed = 0
    for session_dir in sessions_dir.iterdir():
        history_dir = session_dir / "history"
        if not history_dir.is_dir():
            continue
        for f in history_dir.iterdir():
            if not f.suffix == ".md":
                continue
            try:
                d = date.fromisoformat(f.stem)
            except ValueError:
                continue
            if d < cutoff:
                f.unlink()
                removed += 1
    return removed
```

Emits `system_pruned` event with the count.

## Agent guidance in system prompt

The `[Capabilities]` block's guidance section gains:

> When the user says "remember X," "always Y," "never Z," or
> makes a direct correction ("no, use X instead of Y"), call
> `learn_add` to save the lesson. Be concise; one bullet per
> lesson. Use categories sparingly; `tool`, `style`, `pattern`,
> `preference` are typical.

This is a soft prompt; misses are expected. Caught by eval harness
(reuses Phase 4's eval infrastructure).

## `fitt learn` CLI

```
fitt learn list
fitt learn add <text> [--category <cat>]
fitt learn remove <substring>
```

- `list`: show all lessons with index + text. Print total / cap.
- `add`: append a new lesson directly (bypasses approval; CLI =
  user).
- `remove`: substring match; if multiple match, prompt with
  numbered list for selection.

## Configuration additions

### `config.yaml`

```yaml
memory:
  # Existing fields from Phase 2 unchanged.

  # Lessons
  lessons_max_entries: 50
  lessons_block_cap: 3000           # chars in system prompt

  # Decaying history
  today_cap_chars: 24000
  yesterday_cap_chars: 2000
  markers_cap_chars: 1500
  markers_days: 30
  history_max_days: 90              # prune cutoff
```

## Module layout

```
gateway/
  src/gateway/
    lessons.py                      # NEW: Lesson / LessonStore
    memory_decay.py                 # NEW: decaying history reader
    tools/
      lessons.py                    # NEW: learn_add/list/remove tools
    # existing:
    memory_templates.py             # unchanged
    sessions.py                     # unchanged
    cron.py                         # system cron registration added
  tests/
    test_lessons.py                 # NEW
    test_memory_decay.py            # NEW
    test_tools_lessons.py           # NEW
    test_history_prune.py           # NEW
```

## Tests

### Unit

- `test_lessons.py`: load, add, remove, substring dedup, cap
  enforcement, atomic write.
- `test_memory_decay.py`: synthesised `sessions_dir` with N days
  of dummy data; verify each tier (today full, yesterday
  truncated, markers) behaves correctly; caps honored.
- `test_tools_lessons.py`: each tool end-to-end with a fresh
  lessons file.
- `test_history_prune.py`: files dated 0, 30, 60, 90, 120 days
  ago; pruner with `max_age_days=90` leaves 0..90, removes 120.

### Property-based

- **Idempotent add**: adding the same lesson twice produces one
  entry (dedup).
- **Bounded size**: after N `add` calls, file has at most
  `lessons_max_entries` entries.
- **Cap monotonicity**: truncating to a smaller cap is a
  prefix-or-equal operation of truncating to a larger cap.

### Integration

- End-to-end: user says "always use rg" → `learn_add` called →
  `lessons.md` updated → new session sees the lesson in the
  prompt. Verified by building the system prompt and asserting
  the text is present.

## Interactions with other phases

- **Phase 2.5 sessions**: each session has its own history
  directory. Decaying reader is per-session. Lessons are global
  (same for all sessions).
- **Phase 4 tools**: lessons tools are regular Phase 4 tools with
  buckets. Nothing special.
- **Phase 4.5 events**: `learn_add` success could emit an event
  ("learned: X") but it's a user-facing action; noisy. Decision:
  don't auto-emit events for `learn_add`. The user already knew
  when they said "remember." For `learn_remove`, emit an event
  (destructive).
- **Phase 6 spec-runner**: the spec-runner uses lessons + history
  the same way any other session does. No special wiring.

## Rollout

Implementation order:

1. `lessons.py` + `LessonStore` + tests. No tools yet.
2. `learn_add`, `learn_list`, `learn_remove` inline tools.
3. Inject `[Learned corrections]` block in the context builder.
4. `memory_decay.py` + tests. Replace the Phase 2 history
   injection call site.
5. History pruner + system cron registration.
6. `fitt learn` CLI.
7. Agent guidance in system prompt.
8. Live validation.

## Open design decisions

1. **Agent guidance vs. system prompt bloat.** The new guidance
   about `learn_add` adds characters to every request. If the
   model's context window is tight, trim ruthlessly. v0 budget
   is ~200 chars of guidance.

2. **Project-scoped lessons.** Deferred. If the need for "lesson
   X applies to project Y only" becomes common, we add a
   `projects:` array to each Lesson entry and filter at
   injection time. For now, users write the scope into the
   lesson text: "in retro-ai, use tabs."

3. **Lesson timestamp display.** We store `added_ts` for dedup
   ordering but don't display it in `fitt learn list`. Toggleable
   via `--with-dates` if someone wants it.

4. **What's a "category"?** Free text, optional. Not enforced.
   The agent uses it if it wants; typically `tool`, `style`,
   `pattern`, `preference`. We don't restrict or validate.

5. **File watcher?** Not worth it (R5 in requirements). Read on
   every request. Small files, frequent reads, no daemon needed.
