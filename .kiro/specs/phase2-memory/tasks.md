# Tasks: FITT Phase 2 — Memory v0

## Phase 2a — Scaffold

- [ ] 1. Add `memory:` section to the Pydantic config in
  `gateway/config.py` with fields `enabled`, `max_history_chars`,
  `identity_dir`, `sessions_dir` (all with sensible defaults).
- [ ] 2. Update `configs/config.example.yaml` with the new `memory:`
  block and comment explaining each field.
- [ ] 3. Add `MemoryDisabled` and `UnknownSession` exception classes
  to `gateway/errors.py`.

## Phase 2b — The `MemoryStore`

- [ ] 4. Create `gateway/memory.py` with:
  - `LoadedContext` dataclass (system_prefix, history_messages,
    truncated_bytes).
  - `MemoryStore` class with:
    - `__init__(home, identity_dir, sessions_dir, max_history_chars,
      enabled)`.
    - `history_path(session_id, date) -> Path`.
    - `load_context(session_id) -> LoadedContext`.
    - `append_turn(session_id, user_message, assistant_message)`.
    - `_ensure_identity_defaults()` — creates missing identity files
      from templates (called once at MemoryStore construction).
- [ ] 5. Ship default identity templates as string constants in
  `gateway/memory_templates.py` (user, soul, tools). Include
  `<TODO>` markers. Writing the files is side-effectful and happens
  in `_ensure_identity_defaults`.
- [ ] 6. Implement the markdown turn format:
  - `## 2026-04-30T17:42:12Z user\n\n<content>\n\n` followed by the
    matching assistant block.
  - Parser that reads a history file and returns
    `list[ChatMessage]` alternating user/assistant.
- [ ] 7. Implement character-based truncation that drops
  oldest-first, preserving turn boundaries (don't split mid-turn).
- [ ] 8. Unit tests for all of the above (see Testing Strategy in
  design.md).

## Phase 2c — Sessions (minimal v0 surface)

- [ ] 9. Create `gateway/sessions.py` with
  `VALID_SESSION_IDS_V0 = {"main"}` and
  `resolve_session_id(request) -> str`.
- [ ] 10. Register a FastAPI exception handler for `UnknownSession`
  that returns 400 with the documented body shape.
- [ ] 11. Unit tests: missing header defaults to `main`; `main`
  header is accepted; any other value raises `UnknownSession`.

## Phase 2d — Wire memory into `/v1/chat/completions`

- [ ] 12. Instantiate `MemoryStore` once in `create_app` and attach
  to `app.state`.
- [ ] 13. In `chat.py`, before calling `router.dispatch`:
  - Resolve the session id via `resolve_session_id`.
  - Call `memory.load_context(session_id)`.
  - Merge the system prefix into the `messages` system role (or
    prepend a system message if absent).
  - Insert `history_messages` before the incoming user message.
- [ ] 14. After response completion (non-streaming): extract the
  assistant's reply text, call `memory.append_turn`.
- [ ] 15. For streaming: wrap the upstream async generator in a
  collector that accumulates the assistant's `content` deltas.
  After `[DONE]`, call `memory.append_turn`. On mid-stream error,
  do not append.
- [ ] 16. Add request logging fields: `session_id`, `history_chars`,
  `history_truncated_bytes`.
- [ ] 17. Integration tests for the full chat pipeline (identity
  injection, history injection, append after success, no-append on
  error, stream correctness, `X-FITT-Session` header handling).

## Phase 2e — CLI additions

- [ ] 18. Add `fitt memory` command group to `gateway/cli.py`.
- [ ] 19. Implement `fitt memory show --session main [--date YYYY-MM-DD]`
  that prints the rendered context (identity + history) as the LLM
  would see it.
- [ ] 20. Implement `fitt memory append --session main --user "..."
  --assistant "..."` for manual seeding/testing.
- [ ] 21. Implement `fitt memory path --session main` that prints
  today's history file path (handy for tailing, editing).
- [ ] 22. CLI tests.

## Phase 2f — Property tests

- [ ] 23. **Phase 2, Property 2: history ordering** — hypothesis
  generates random turn sequences; assert round-trip order across
  `append_turn` + `load_context`.
- [ ] 24. **Phase 2, Property 3: append atomicity** — hypothesis
  generates interleaved successful + error-inducing turns; assert
  no unmatched user-without-assistant entries exist.
- [ ] 25. **Phase 2, Property 4: truncation keeps most recent** —
  hypothesis generates history of random size against random
  budget; assert loaded slice is a suffix.

## Phase 2g — The "keys on the counter" integration test

- [ ] 26. `test_keys_on_the_counter_across_restart` in
  `tests/test_memory_integration.py`:
  - Fixture that creates a fresh `FITT_HOME` tmp dir with default
    identity templates.
  - Mock an OpenAI-compatible upstream that echoes back the last
    user message verbatim for verifiability.
  - Build app #1, POST "remember: my keys are on the counter",
    assert append happened.
  - Dispose of app #1 completely.
  - Build app #2 against the same `FITT_HOME`.
  - POST "where are my keys?", capture the request body sent to
    the mock upstream, assert "counter" appears in the messages
    passed to it.

## Phase 2h — Documentation

- [ ] 27. Update `gateway/README.md`:
  - Add "Memory" section to the config reference.
  - Add `fitt memory show / append / path` to the CLI reference.
  - Mention `X-FITT-Session` header in the HTTP API section (even
    though only `main` is accepted in v0).
- [ ] 28. Update `docs/quickstart.md`:
  - Brief mention at the end that memory now persists across
    restarts and where the files live.
- [ ] 29. Update `.kiro/steering/project-overview.md` with "Phase 2
  complete" after all above are done.

## Exit Criteria

- The keys-on-the-counter test passes reliably.
- Every request logs a `session_id` and history metadata.
- `fitt memory show` prints a human-readable rendering of what FITT
  remembers.
- `ruff check`, `ruff format`, `mypy src` all clean.
- No regression: all Phase 1 tests still pass.

## Non-Goals (repeated from requirements)

- No cross-day history (Phase 7).
- No vector search, no Mem0 (Phase 7).
- No `fitt session` CLI (Phase 2.5).
- No multi-session concurrent support in v0 (`main` only).
- No automatic summarization (Phase 7).
