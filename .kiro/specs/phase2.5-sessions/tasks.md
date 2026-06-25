# Tasks: FITT Phase 2.5 — Sessions

**Status:** shipped

## Phase 2.5a — SessionRegistry

- [ ] 1. Replace `VALID_SESSION_IDS_V0` + module-level
  `resolve_session_id` in `gateway/sessions.py` with:
  - `Session` dataclass (`id`, `name`, `created_at`, `archived`).
  - `SessionRegistry` class with `all`, `get`, `valid_ids`,
    `create`, `rename`, `archive`, `unarchive`, and an internal
    `_load` / `_save` pair.
  - A new module-level `resolve_session_id(request, registry)`.
  - `SESSION_ID_PATTERN` regex.
- [ ] 2. Atomic write helper (`tempfile.NamedTemporaryFile` +
  `os.replace`) for `sessions.json` updates.
- [ ] 3. `ensure_main()` called during registry construction.
- [ ] 4. Unit tests (see Testing Strategy).

## Phase 2.5b — Gateway integration

- [ ] 5. Build `SessionRegistry` in `create_app`, store on
  `app.state.session_registry`.
- [ ] 6. Update `chat.py` to call the new resolver signature.
- [ ] 7. Integration tests:
  - chat with custom session header routes correctly,
  - archived session returns 400,
  - unknown session returns 400 with list of active ids,
  - history isolation across two sessions (Property 4),
  - new session visible to the next request without restart
    (Property 5).

## Phase 2.5c — CLI

- [ ] 8. Add `fitt session` click group with subcommands:
  - `list [--include-archived]`
  - `new <id> [--name NAME]`
  - `rename <id> --name NAME`
  - `archive <id>`
  - `unarchive <id>`
  - `path <id>`
- [ ] 9. CLI tests (see Testing Strategy).

## Phase 2.5d — Documentation

- [ ] 10. Update `gateway/README.md`:
  - Add "Sessions" section under config/runtime.
  - Document the `fitt session` CLI.
  - Document `X-FITT-Session` header usage.
- [ ] 11. Update `docs/quickstart.md`:
  - Brief mention at the end of Phase 2 summary that named
    sessions exist for scoping.
- [ ] 12. Update `.kiro/steering/project-overview.md` with "Phase
  2.5 complete" once all above are done.

## Exit criteria

- `fitt session new retroai` creates the session.
- `curl -H "X-FITT-Session: retroai" ...` routes correctly.
- History written to `retroai` does not appear in `main`'s context
  and vice versa.
- `fitt session archive retroai` blocks subsequent requests to
  that id.
- `fitt session archive main` errors out.
- All tests pass, ruff + mypy clean.

## Non-Goals (repeated from requirements)

- No auto-session creation from user-provided ids.
- No per-interface default-session routing (Phase 3 decides that).
- No session-scoped identity.
- No session export/import.
