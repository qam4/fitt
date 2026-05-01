# Phase 5 — Lessons + Decaying History: Tasks

Status legend: `[x]` done, `[ ]` not yet.

## 1. Lesson store

- [ ] 1a. `gateway/lessons.py`: `Lesson` dataclass (id, text,
       category, added_ts) and `LessonStore` class.
- [ ] 1b. File format reader: parse markdown bullets,
       continuation lines, `[category: xxx]` tags. Skip blank
       lines and comments. Malformed bullets logged and skipped.
- [ ] 1c. File format writer: round-trip preserves structure.
       Atomic writes (tmp + rename).
- [ ] 1d. `add(text, category)`:
       - Truncate `text` at `MAX_LESSON_CHARS`.
       - Substring dedup: new is substring of existing, or vice
         versa, replace older with newer.
       - Cap at `memory.lessons_max_entries`; drop oldest when
         full.
- [ ] 1e. `remove_matching(substring)` → count of removed.
- [ ] 1f. `get_context(cap_chars)` → formatted `[Learned
       corrections]` block, oldest-first truncated to cap.
- [ ] 1g. Tests: `test_lessons.py`. Cover parsing, writing,
       dedup, cap enforcement, cap truncation.

## 2. Lessons inline tools

- [ ] 2a. `gateway/tools/lessons.py`: `learn_add`, `learn_list`,
       `learn_remove`.
- [ ] 2b. Register with the Phase 4 tool registry.
       - `learn_add`: bucket `auto`.
       - `learn_list`: bucket `auto`.
       - `learn_remove`: bucket `ask`.
- [ ] 2c. Tests: `test_tools_lessons.py`.

## 3. Lessons injection in the system prompt

- [ ] 3a. Update the context builder (wherever it lives — likely
       in `gateway/chat.py` or a dedicated context module) to
       read lessons and include the `[Learned corrections]`
       block between `[Capabilities]` and `[Session history]`.
- [ ] 3b. Respect `memory.lessons_block_cap`.
- [ ] 3c. Tests: system prompt assembly with and without lessons.

## 4. Decaying history reader

- [ ] 4a. `gateway/memory_decay.py`: `HistoryContext` dataclass
       + `build_history_context` function per design.
- [ ] 4b. Implementations:
       - Today full, truncated from oldest if > `today_cap_chars`.
       - Yesterday: first entry + count.
       - Markers: days 3 .. `markers_days` ago, one line each.
- [ ] 4c. Respect all caps.
- [ ] 4d. Tests: `test_memory_decay.py` with synthesised
       fixtures spanning 35 days.

## 5. Replace Phase 2 history injection

- [ ] 5a. Find the current call site in the chat handler where
       Phase 2 loaded "today only" and replace with the new
       `build_history_context`.
- [ ] 5b. Update the system prompt template to use the three
       sections (today / yesterday / older markers).
- [ ] 5c. Update tests that assert on system-prompt shape.

## 6. History pruning

- [ ] 6a. `gateway/memory_decay.py::prune_history(sessions_dir,
       max_age_days)` function. Returns count of files deleted.
- [ ] 6b. Register an internal cron at gateway startup:
       - `id = "system_history_prune"`
       - `schedule = "0 4 * * *"` (04:00 daily)
       - `silent = true`
       - `system = true` (hidden from `cron_list`)
       - Callback: `prune_history` + emit a `system_pruned` event.
- [ ] 6c. Tests: `test_history_prune.py`.

## 7. `fitt learn` CLI

- [ ] 7a. `fitt learn list` — numbered print with count vs cap.
- [ ] 7b. `fitt learn add <text> [--category <cat>]` — direct
       write via `LessonStore.add`.
- [ ] 7c. `fitt learn remove <substring>` — multi-match prompt
       for selection.
- [ ] 7d. Tests.

## 8. Agent guidance in system prompt

- [ ] 8a. Add a short paragraph to the `[Capabilities]` block
       explaining when to call `learn_add`. Keep under 200 chars
       of new content.
- [ ] 8b. Update eval harness: add sample prompts that should
       produce a `learn_add` call and sample prompts that should
       not. Measure.

## 9. Config additions

- [ ] 9a. Extend the `memory:` section of `config.example.yaml`
       with the new fields (`lessons_max_entries`,
       `lessons_block_cap`, `today_cap_chars`, `yesterday_cap_chars`,
       `markers_cap_chars`, `markers_days`, `history_max_days`).
- [ ] 9b. Pydantic model updates with sensible defaults.

## 10. Docs

- [ ] 10a. Gateway README: new section on lessons and decaying
       history.
- [ ] 10b. Quickstart: mention `fitt learn` and how to teach
       patterns.

## 11. Live validation

(Manual.)

- [ ] 11a. Tell FITT via Telegram "always use `rg` instead of
       `grep` in shell commands." Verify `learn_add` was called,
       `lessons.md` updated.
- [ ] 11b. Start a new session. Ask the agent to "search the repo
       for TODO." Verify it uses `rg`.
- [ ] 11c. Walk FITT through the RL-monitoring pattern once.
       Verify a lesson is captured.
- [ ] 11d. Next day, say "monitor training pid 456." Verify it
       creates a cron without re-explaining.
- [ ] 11e. Edit `lessons.md` by hand. Verify changes show up in
       the next request.
- [ ] 11f. `fitt learn list` / `add` / `remove` work from the
       CLI.
- [ ] 11g. After 30+ days of use: verify history markers show up
       for old days; today is full; 30+ day files still on disk
       but not in prompt.
- [ ] 11h. Pruning: leave a stale history file dated 100 days
       ago (fake it with `touch -d`), wait for 04:00 cron (or
       fire manually with `fitt cron run system_history_prune`),
       verify the file is gone and a `system_pruned` event was
       emitted.

## Definition of done

- All required `[ ]` above complete.
- `uv run pytest -q` passes.
- Live validation (11a-11h) all green.
- Author has used Phase 5 for 1 week without wanting to revert.
