# Design: FITT Phase 2.5 — Sessions

## Architecture

Phase 2.5 broadens session resolution. Phase 2's
`VALID_SESSION_IDS_V0 = {"main"}` becomes a dynamic set loaded from
the session index on disk.

```
~/.fitt/
  sessions/
    sessions.json          # index (new in Phase 2.5)
    main/                  # session dir (Phase 2 created this)
      history/
        YYYY-MM-DD.md
    retroai/               # user-created in 2.5
      history/
        YYYY-MM-DD.md
    experiments/           # another user-created session
      history/
        YYYY-MM-DD.md
```

`sessions.json`:

```json
{
  "version": 1,
  "sessions": [
    {
      "id": "main",
      "name": "Main",
      "created_at": "2026-04-30T10:00:00Z",
      "archived": false
    },
    {
      "id": "retroai",
      "name": "Retro-AI",
      "created_at": "2026-05-03T18:14:22Z",
      "archived": false
    }
  ]
}
```

## Key Design Decisions

### Decision 1: JSON index, not a database

Consistent with Phase 2 (markdown files, no DB). The index is a tiny
JSON file that pydantic can validate and the user can edit by hand if
something goes wrong.

### Decision 2: Fresh-read on every request

The session resolver re-reads `sessions.json` on every request (one
cheap file read). This means `fitt session new` takes effect
immediately without a gateway restart. The cost is negligible for
personal-use volumes; if it ever matters, a simple mtime cache is
trivial to add.

### Decision 3: Strict id validation

Ids are lowercase, digits, and hyphens only. No slashes, no
uppercase, no unicode. The id becomes a directory name; restricting
it prevents path-traversal mistakes and keeps filesystem behavior
predictable on both Windows and POSIX.

### Decision 4: Archived sessions stay on disk

Archive is not delete. History is small and valuable. An archived
session is hidden from listings and blocked for new chat requests,
but nothing is moved or removed.

### Decision 5: `main` is untouchable

The invariant "there is always at least one active session called
main" is enforced. Any attempt to delete, archive, or rename `main`
returns an error.

## Module Design

### `gateway/sessions.py` (expanded)

```python
@dataclass(frozen=True)
class Session:
    id: str
    name: str
    created_at: datetime
    archived: bool = False


class SessionRegistry:
    """On-disk session index. Re-reads every request."""

    def __init__(self, sessions_dir: Path):
        self._sessions_dir = sessions_dir
        self._index_path = sessions_dir / "sessions.json"

    def ensure_main(self) -> None:
        """Create main if missing. Called once at startup."""

    def all(self, *, include_archived: bool = False) -> list[Session]: ...
    def get(self, session_id: str) -> Session | None: ...
    def valid_ids(self) -> set[str]: ...   # active only
    def create(self, session_id: str, name: str | None) -> Session: ...
    def rename(self, session_id: str, name: str) -> Session: ...
    def archive(self, session_id: str) -> Session: ...
    def unarchive(self, session_id: str) -> Session: ...


def resolve_session_id(request: Request, registry: SessionRegistry) -> str:
    """Phase 2.5: read registry, validate against active ids."""
```

### `gateway/app.py`

Create the `SessionRegistry` at app startup, ensure `main` exists,
store on `app.state`. Wire it into the chat handler.

### `gateway/chat.py`

The resolver call changes from `resolve_session_id(request)` to
`resolve_session_id(request, request.app.state.session_registry)`.
Otherwise unchanged.

### `gateway/cli.py`

Add `fitt session` group:

```
fitt session list [--include-archived]
fitt session new <id> [--name "Display"]
fitt session rename <id> --name "..."
fitt session archive <id>
fitt session unarchive <id>
fitt session path <id>       # print the session's history dir
```

All commands use the same `SessionRegistry` class, instantiated from
the config's `sessions_dir`.

## Failure handling

| Scenario                                | Behavior                                          |
|-----------------------------------------|---------------------------------------------------|
| `sessions.json` missing                 | Create with just `main`.                          |
| `sessions.json` corrupted (bad JSON)    | Log warning, proceed with in-memory `main` only.  |
| Id regex mismatch                       | CLI: non-zero exit + message. HTTP: N/A (header validated at runtime). |
| Attempt to create duplicate id          | CLI: non-zero exit. HTTP: N/A.                    |
| Attempt to archive/rename `main`        | CLI: non-zero exit with a clear error.            |
| Chat request names archived session     | 400 `unknown_session`, list active ids.           |
| Chat request names unknown session      | 400 `unknown_session`, list active ids.           |

## Correctness Properties

### Property 1: Main invariant

*At any point* after registry initialisation, `main` is always in
`all()` and `valid_ids()`, and `archived=false`.

**Validates: 1.6, 4.2**

### Property 2: Id validation

*For any* attempt to create a session with an id that doesn't match
the regex, `create()` raises without touching the index file.

**Validates: 1.2**

### Property 3: Archive is not delete

*For any* session, after `archive()` the session remains in
`all(include_archived=True)` with `archived=True`, but is NOT in
`valid_ids()`.

**Validates: 1.4, 4.1**

### Property 4: History isolation

*For any* two sessions A and B, writes to A's history file do not
appear in B's loaded context, and vice versa.

**Validates: 3.1, 3.2, 3.3**

### Property 5: Fresh-read

*For any* session created via `create()` (or via direct edit of
`sessions.json`), the next `resolve_session_id()` call accepts the
new id without any gateway restart.

**Validates: 2.4**

## Testing Strategy

### Unit tests

- `test_ensure_main_creates_index_if_missing`
- `test_ensure_main_preserves_existing_sessions`
- `test_create_session_adds_to_index`
- `test_create_rejects_bad_id` (uppercase, slashes, empty, etc.)
- `test_create_rejects_duplicate_id`
- `test_rename_updates_name_only`
- `test_archive_hides_from_valid_ids`
- `test_unarchive_restores`
- `test_archive_main_raises`
- `test_rename_main_raises`
- `test_corrupted_index_falls_back_to_main_only`

### Integration tests

- `test_chat_with_custom_session_header`: create session, POST with
  its id, assert correct routing.
- `test_chat_archived_session_returns_400`
- `test_chat_default_main_when_no_header`
- `test_history_isolation_across_sessions` (Property 4): chat in
  session A, then session B, verify B's context doesn't contain A's
  content.
- `test_new_session_visible_without_restart`: POST returns 400 for
  `foo`; CLI creates `foo`; POST succeeds for `foo`.

### CLI tests

- `test_cli_session_list_empty_shows_main`
- `test_cli_session_new_valid`
- `test_cli_session_new_invalid_id`
- `test_cli_session_archive_and_list`
- `test_cli_session_rename_preserves_history_path`

## Known concerns

- **Concurrent writes to `sessions.json`**. Two CLI invocations at
  the same time could race. v0 accepts last-write-wins; the CLI
  reads, edits in memory, writes back. Documented, not mitigated.
  File locking can be added if it ever matters.
- **No atomic rename**. A crash mid-write leaves a half-written
  sessions.json. Mitigation: write-then-rename pattern via
  `tempfile.NamedTemporaryFile` + `os.replace`.

## Future extensions

- Per-interface default-session logic (Phase 3 Telegram: "DM to bot
  goes to main; replies in a group thread go to that thread's
  session").
- Session-scoped identity overrides (Phase 7+).
- Session export / import as a single tarball.
