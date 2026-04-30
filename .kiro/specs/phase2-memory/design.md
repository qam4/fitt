# Design: FITT Phase 2 — Memory v0

## Architecture

Phase 2 adds one module (`gateway/memory.py`), one request-pipeline
hook (before dispatch), and one response-pipeline hook (after
completion). Everything else in the gateway is unchanged.

```
                 ┌──────────────────────────────┐
POST /v1/chat    │ chat.py endpoint             │
─────────────────▶                              │
  messages:      │  1. parse + validate         │
    [...user]    │  2. resolve session id       │◄── X-FITT-Session (default: main)
                 │  3. memory.load_context()    │─┐
                 │  4. router.dispatch()        │ │  reads identity/*.md
                 │  5. await response           │ │  reads sessions/<id>/history/YYYY-MM-DD.md
                 │  6. memory.append_turn()     │─┤  appends to same file
                 │  7. return to client         │ │
                 └──────────────────────────────┘ │
                                                  │
                 ┌──────────────────────────────┐ │
                 │ gateway/memory.py            │◄┘
                 │                              │
                 │  load_context(session_id):   │
                 │    merge identity + history  │
                 │                              │
                 │  append_turn(session_id,     │
                 │              user, assistant)│
                 └──────────────────────────────┘
                        │
                        ▼
  ~/.fitt/
     identity/
       user.md         # injected into every system prompt
       soul.md         # FITT personality
       tools.md        # known-tool summary (Phase 4 auto-populates)
     sessions/
       main/
         history/
           2026-04-30.md    # today, append-only
           2026-04-29.md    # yesterday, untouched (Phase 7 uses)
```

## Key Design Decisions

### Decision 1: Markdown files, not a database

**Rationale**: transparency, portability, debuggability. You can
`cat` the file and see exactly what FITT remembers. You can edit it
in any text editor. There's no schema migration when Phase 7 adds
RAG - the vectors will index the same markdown. Cost: a later phase
will need compaction when files grow large, but that's deferred.

### Decision 2: Session-aware storage from day one

**Rationale**: explained in requirements §4. Phase 2.5 will add
`fitt session` and explicit session selection in Telegram. If Phase
2 used a flat `history/YYYY-MM-DD.md`, Phase 2.5 would have to
migrate. Making every file path `sessions/<id>/history/...` from the
start makes Phase 2.5 purely additive.

### Decision 3: Today-only, oldest-truncation

**Rationale**: today + yesterday loading, decay, and compaction are
real problems but they all want real design work (not "which line do
we drop when we hit 24k chars"). Deferring them to Phase 7 keeps
Phase 2 small. Oldest-first truncation is a sensible placeholder
that won't surprise anyone.

### Decision 4: Write after response, not during streaming

**Rationale**: a streamed response that errors out mid-stream should
not corrupt the history file. Writing on completion means either the
response landed fully or it didn't get persisted. The cost is that a
crash after response-complete but before write-to-disk loses one
turn - acceptable.

For non-streaming responses, the write happens after the LLM call
returns but before the HTTP response is sent to the client.

For streaming responses, the write happens in a `finally` block
around the stream generator, after the generator is exhausted
cleanly. A mid-stream error short-circuits out without writing.

### Decision 5: Identity is shared across sessions

**Rationale**: identity is "who you are" and "who FITT is." It
doesn't vary by project. A Retro-AI session and a home-automation
session both address the same user. Phase 2.5's named sessions will
load the same `identity/*.md` files plus their own history.

### Decision 6: Context budget is character-based, not token-based

**Rationale**: exact tokenization depends on the model. A character
count is an approximation (~4 chars per English token) but it's
deterministic, model-agnostic, and cheap. Users can tune
`memory.max_history_chars` for their backend. Phase 7 can add proper
token-aware budgeting if needed.

## Module Design

### `gateway/memory.py`

```python
class MemoryStore:
    """Session-scoped markdown memory on disk."""

    def __init__(self, home: Path, max_history_chars: int, enabled: bool):
        self._home = home
        self._max = max_history_chars
        self._enabled = enabled

    # --- public API --------------------------------------------

    def load_context(self, session_id: str) -> LoadedContext:
        """Return identity + today's history, ready to inject.

        Returns empty LoadedContext if disabled.
        Truncates oldest turns if history exceeds max_history_chars.
        """

    def append_turn(
        self,
        session_id: str,
        user_message: str,
        assistant_message: str,
    ) -> None:
        """Append a user/assistant turn pair to today's history."""

    def history_path(self, session_id: str, date: date) -> Path:
        """Helper: path for a given session + date."""


@dataclass(frozen=True)
class LoadedContext:
    system_prefix: str             # identity/*.md merged
    history_messages: list[dict]   # [{role, content}, ...]
    truncated_bytes: int           # for logging
```

### Integration into `gateway/chat.py`

```python
# Current Phase 1 flow (simplified):
#   parse -> dispatch -> respond

# Phase 2 flow:
#   parse
#   -> resolve_session_id()
#   -> ctx = memory.load_context(session_id)
#   -> inject ctx.system_prefix into system message
#   -> prepend ctx.history_messages before user message
#   -> dispatch (unchanged)
#   -> await response
#   -> memory.append_turn(session_id, user_msg, assistant_msg)
#   -> return
```

Two concrete points in `chat.py`:

1. **Before `router.dispatch`**: wrap the incoming `messages` list
   with the system prefix and history injection.
2. **After response completion**: call `memory.append_turn`. For
   streaming, wrap the stream generator with a collector that
   captures the full assistant response for the append call.

### `gateway/sessions.py` (new, minimal)

In Phase 2, only enough session support to route to the right file.
Phase 2.5 expands this.

```python
VALID_SESSION_IDS_V0 = {"main"}

def resolve_session_id(request: Request) -> str:
    """Phase 2: main only. Phase 2.5 expands to arbitrary names."""
    sid = request.headers.get("X-FITT-Session", "main")
    if sid not in VALID_SESSION_IDS_V0:
        raise UnknownSession(sid)
    return sid
```

### `gateway/cli.py` additions

Three new subcommands under `fitt memory`:

```
fitt memory show [--session main] [--date YYYY-MM-DD]
fitt memory append --session main --user "..." --assistant "..."
fitt memory path --session main     # print the today's history file path
```

## Configuration

Additions to `~/.fitt/config.yaml`:

```yaml
memory:
  enabled: true
  max_history_chars: 24000
  # Where identity and history live. Default: FITT_HOME/identity and
  # FITT_HOME/sessions. Overridable if you want identity on a shared
  # volume and history local.
  identity_dir: ~/.fitt/identity
  sessions_dir: ~/.fitt/sessions
```

All four fields optional with sensible defaults. No new entries in
`secrets.yaml`.

## Identity default templates

The gateway creates these on first startup if missing. The
templates include `<TODO>` markers so users know what to fill in.

**`identity/user.md`**:

```markdown
# About Me

<TODO: your name, role, core context the assistant should always know>

## Projects I'm working on

<TODO: one or two short lines per project>

## Preferences

<TODO: how you like responses formatted, tone, level of detail>
```

**`identity/soul.md`**:

```markdown
# Your Role

You are FITT, a personal AI assistant running on the user's own
hardware. You are honest, concise, and direct. You admit when you
don't know something or when you lack the tool to complete a
request.

When the user asks for something you cannot do because a tool is
missing, say what tool is missing and suggest how to add it. Never
hallucinate an action.

## Tone

Match the user's register. Be warm but not chatty; thorough but not
long-winded.
```

**`identity/tools.md`**:

```markdown
# Tools Available

You currently do NOT have tool access. You can:
- read and reason over text the user provides
- answer questions from your training data
- remember what was said earlier today in this conversation

You CANNOT (yet):
- access the filesystem
- run shell commands
- call external APIs
- search the web

In Phase 4, tool access via MCP will be added. Until then, if a
request requires one of the above, say so and suggest the user
provide the information directly.
```

## Failure handling

| Scenario                                    | Behavior                                                                         |
|---------------------------------------------|----------------------------------------------------------------------------------|
| `identity/` missing                         | Create from templates on startup. Warn in logs.                                  |
| `sessions/<id>/history/YYYY-MM-DD.md` missing | Proceed with no history. Not an error.                                         |
| Identity file unreadable (permission, IOError) | Skip that file, log warning, continue with the others.                        |
| History file unreadable                     | Log warning, proceed with empty history. Don't fail the request.                |
| History file corrupted (unparseable)        | Log warning naming the file. Proceed with empty history. File is not modified. |
| History write fails after response          | Log error. Return the response to the client successfully. Next turn retries.  |
| `X-FITT-Session` set to unknown value       | 400, JSON body with `type: unknown_session` and the invalid id.                 |

## Correctness Properties

### Property 1: Identity idempotence

*For any* number of `GET /v1/chat/completions` calls without editing
identity files, the system prefix injected into each request is
byte-identical.

**Validates: 1.3**

### Property 2: History ordering

*For any* sequence of `N` completed turns, the history file contains
the same `N` user+assistant pairs in the order they happened, and
the loaded context's `history_messages` list is the same order.

**Validates: 2.1, 2.2, 2.3**

### Property 3: Append atomicity

*For any* successful response completion, either the full
`user`/`assistant` pair is appended to disk, or neither is. A
partial write (user but no assistant) never happens.

**Validates: 2.6**

### Property 4: Truncation keeps most recent

*For any* history file exceeding `max_history_chars`, the loaded
context's `history_messages` contains a contiguous suffix of the
on-disk turns (the most recent ones), not a prefix or middle.

**Validates: 5.2**

### Property 5: Session isolation

*For any* two distinct session ids (once Phase 2.5 enables them),
writes to session A do not appear in session B's loaded context,
and vice versa.

**Validates: 4.3** (prospectively; the tests exist in Phase 2
parameterised on the not-yet-enabled "other" session id, so Phase
2.5 can flip a flag and the tests verify isolation immediately.)

### Property 6: Cross-restart persistence (keys on the counter)

*For any* turn that was successfully appended to today's history,
a subsequent gateway process (fresh import, no in-memory state)
loads that turn's content into the next request's context.

**Validates: 3.1**

## Testing Strategy

### Unit tests

- `test_identity_templates_created_on_first_load` - missing dir
  populated from defaults.
- `test_identity_reload_on_edit` - editing a file between two
  `load_context` calls produces different output.
- `test_history_file_path_session_scoped` - path always under
  `sessions/<id>/history/`.
- `test_append_turn_creates_parent_dirs` - session dir doesn't
  exist, append creates it.
- `test_append_turn_formats_iso_timestamp` - regex check on
  output.
- `test_load_context_empty_history_returns_empty_list` - no file.
- `test_load_context_returns_turns_in_order` - three turns
  appended, loaded back in the same order.
- `test_truncation_drops_oldest` - history exceeds budget, most
  recent turns are kept.
- `test_truncation_reports_dropped_bytes` - `truncated_bytes > 0`.
- `test_disabled_returns_empty_context` - `enabled: false`
  short-circuits.

### Integration tests (with `respx` mocks)

- `test_chat_injects_identity_into_system` - mock upstream, assert
  the request body's system message contains identity content.
- `test_chat_injects_history_into_messages` - second turn sees
  first turn's content.
- `test_chat_appends_turn_after_response` - after a successful
  chat, file on disk has the turn.
- `test_chat_does_not_append_on_upstream_error` - when dispatch
  raises, no file write happens.
- `test_chat_streaming_appends_only_after_full_stream` - streamed
  response's content is captured and appended after `[DONE]`.
- `test_chat_streaming_error_mid_stream_does_not_append` -
  simulated drop, no write.
- `test_chat_rejects_unknown_session_id` - `X-FITT-Session: foo`
  returns 400 in v0.

### Property tests (hypothesis)

- **Phase 2, Property 2: history ordering** - generate random turn
  sequences, assert round-trip order.
- **Phase 2, Property 4: truncation keeps most recent** - generate
  history of random size, budget of random size, verify the loaded
  slice is a suffix of the on-disk content.
- **Phase 2, Property 3: append atomicity** - generate sequences of
  interleaved successful + failing appends, assert no "user without
  matching assistant" entries end up on disk.

### "Keys on the counter" integration test

End-to-end: `test_keys_on_the_counter_across_restart`.

1. Use a tmp `FITT_HOME` with clean identity defaults.
2. Build a test app with a mocked upstream that echoes the last user
   message back.
3. Send turn 1: "remember: my keys are on the counter". Assert
   response persisted.
4. Destroy the app instance (simulates gateway restart).
5. Build a fresh app against the same `FITT_HOME`.
6. Send turn 2: "where are my keys?" Assert the request sent to the
   upstream includes turn 1's content in `messages`.

This test is the canonical Phase 2 validation. If it passes, memory
works.

## Known concerns

- **History file grows unbounded.** Today only, so the file resets at
  midnight UTC. Still, a heavy chat day can produce a large file.
  Mitigation: `max_history_chars` truncation on load. Compaction is
  Phase 7.
- **Markdown format drift.** If a future version changes the turn
  format, old files may parse weirdly. Mitigation: the parser is
  permissive; unrecognized lines become part of whichever turn they
  fall in. Phase 7 can add an explicit version marker.
- **Qwen 14B sometimes ignores system prompt content.** Local models
  vary in how well they use injected memory. Measured behavior is
  acceptable; Phase 7's RAG will help for fact retrieval.

## Future extensions (Phase 2.5+)

- Phase 2.5 adds `fitt session {new,list,rename,archive}` and
  header/param session selection beyond `main`.
- Phase 7 adds yesterday+older loading with summarization, vector
  retrieval across all sessions, and a memory-backup script.
- Phase 4 auto-populates `identity/tools.md` from the loaded MCP
  registry.
