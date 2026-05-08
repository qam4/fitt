# Phase 5 — Lessons + Decaying History: Tasks

Status legend: `[x]` done, `[ ]` not yet.

## 1. Spec promotion

- [x] 1a. Promote Phase 5 from `FITT_ROADMAP.md` inline
       draft to the three-file spec here:
       `requirements.md`, `design.md`, `tasks.md`.
- [x] 1b. Commit the spec as its own change before any
       code lands, matching the Phase 4.5 / 4.6 / 4.7
       convention.

## 2. LessonsStore (plumbing only, no wiring yet)

- [x] 2a. `gateway/src/gateway/lessons.py`: `Lesson`
       dataclass, `LessonsStore` class with `read`,
       `add`, `remove`, `render_block`, `path`,
       `max_entries` properties.
- [x] 2b. Persistence at `$FITT_HOME/identity/lessons.md`
       with template + "Active lessons" section parsed
       and mutated write-through under a threading lock.
- [x] 2c. `max_entries` ceiling (default 50) drops oldest.
- [x] 2d. 25 unit tests (`tests/test_lessons_store.py`).

## 3. Lessons injection into the system prompt

- [x] 3a. `MemoryStore._load_lessons_block()` delegates to
       the store's `render_block`; empty when no lessons.
- [x] 3b. `load_context` composes identity +
       `[Learned corrections]` block in that order.
- [x] 3c. 6 unit tests (`tests/test_memory_lessons_injection.py`).

## 4. `learn_*` inline tools

- [x] 4a. `gateway/src/gateway/tools/lessons.py`:
       `learn_add` / `learn_list` / `learn_remove` with
       the right buckets (list=auto, add/remove=ask).
- [x] 4b. Registered via `build_lessons_tools()`.
- [x] 4c. `ToolContext` grows `lessons: Any = None`.
       Wired in `chat.py` + `cron_runner.py`.
- [x] 4d. 12 unit tests (`tests/test_tools_lessons.py`).

## 5. `fitt learn` CLI

- [x] 5a. `@main.group("learn")` mirroring `fitt cron`.
- [x] 5b. `list` / `add` / `remove` / `path` subcommands.
- [x] 5c. 8 tests (`tests/test_cli_learn.py`).

## 6. Tool-turn persistence format

- [x] 6a. `_HEADER_RE` extended to match
       `assistant tool_calls` / `tool <name>` / `system`.
- [x] 6b. `PersistedToolCall` dataclass with
       `tool_name` / `args_summary` / `result_status` /
       `result_summary` + `render_call_bullet` and
       `render_result_body`.
- [x] 6c. `append_turn` grows `tool_calls: list[
       PersistedToolCall] | None = None`.
- [x] 6d. Parser handles new headers; unknown roles
       degrade gracefully.
- [x] 6e. `_turns_to_messages` reconstructs OpenAI-shape
       messages with deterministic `tool_call_id` hashes.
- [x] 6f. 12 unit tests (`tests/test_memory_tool_turns.py`).

## 7. Wire `PersistedToolCall` collection in call sites

- [x] 7a. `AgentLoopResult` gains `tool_calls_for_memory:
       list[PersistedToolCall]`.
- [x] 7b. `chat.py` passes the list to `append_turn`.
- [x] 7c. `cron_runner.py` same.
- [x] 7d. `detach.py` same.
- [x] 7e. Pre-existing lifecycle tests stay green.

## 8. Decaying history injection

- [x] 8a. `_load_decaying_history` replaces
       `_load_and_truncate_history` in `load_context`.
       Walks three layers: past activity summary,
       yesterday first-turn+count, today in full.
- [x] 8b. Summary text deterministic
       (`YYYY-MM-DD: N user turns (with tools|chat only)`).
- [x] 8c. Layered budget: oldest layer drops first.
- [x] 8d. Summaries render as one `[Past activity]`
       system message prepended to the turn list.
- [x] 8e. 10 unit tests (`tests/test_memory_decay.py`).

## 9. History pruner

- [x] 9a. `gateway/src/gateway/history_pruner.py`
       mirrors `EventPruner`.
- [x] 9b. Walks `sessions/*/history/*.md`, deletes past
       retention. Non-date filenames preserved.
- [x] 9c. Emits `system_pruned` with
       `meta.target="history"`.
- [x] 9d. Anchor file for restart-safe cadence.
- [x] 9e. Wired into `create_app` start/stop.
- [x] 9f. 9 unit tests (`tests/test_history_pruner.py`).

## 10. E2E + regression

- [x] 10a. `tests/e2e/test_lessons_lifecycle.py`:
       `learn_add` → next request dispatches with the
       lesson bullet in the system prompt. Plus the
       counter-case: `learn_remove` removes the bullet.
- [x] 10b. `tests/e2e/test_session_poisoning_lifecycle.py`
       flipped off `xfail` and passes. Assertion
       rewritten to pin the new contract: a tool-using
       turn's structured outcome (`role=tool` with
       `ok`) reaches the model alongside the
       paraphrased reply, so future context carries
       ground truth rather than only the NL.

## 11. Config + docs

- [x] 11a. `MemoryConfig` grows `max_lessons` (default
       50) and `history_max_days` (default 90).
- [x] 11b. Updated `configs/config.example.yaml` with the
       new fields + inline docs explaining the decay
       layers and pruner.
- [ ] 11c. Brief notes in `docs/`. Deferred — the roadmap
       inline description plus the spec's design.md
       cover the shape for now; a dedicated docs page
       earns its place when someone hits the "what does
       the decay layer look like in practice" question
       more than once.

## 12. Roadmap pointer update

- [x] 12a. Flipped the Phase 5 inline draft's "to be
       written" footnote to "Spec promoted 2026-05-08;
       implementation shipped 2026-05-08 ..." with the
       binary-gate (session-poisoning xfail flip) called
       out.
- [ ] 12b. Mark Phase 5 DONE in the roadmap once live
       validation lands.

## 13. Live validation

(Manual.)

- [ ] 13a. From Telegram: "remember: always use uv."
       Approve `learn_add`. Next turn: ask something
       unrelated. Inspect: does the `[Learned
       corrections]` block in the dispatched system
       prompt actually contain the lesson?
       (`docker compose logs gateway --tail 50 | grep
       Learned` as an approximation — the full
       dispatched prompt isn't logged, but the
       request-builder logs the block length.)
- [ ] 13b. `docker compose exec gateway fitt learn
       list` shows the lesson.
- [ ] 13c. From Telegram: "forget about uv." Approve
       `learn_remove`. `fitt learn list` shows 0
       entries.
- [ ] 13d. From Telegram: run `project_shell` with a
       failing command. Next turn: ask a follow-up.
       Confirm the tool's exit-code outcome made it
       into memory (inspect
       `docker compose exec gateway cat
       /root/.fitt/sessions/main/history/<today>.md`).
       The `## <ts> tool project_shell` block should
       show `exit=N: <brief>`, NOT the assistant's
       natural-language paraphrase.
- [ ] 13e. Wait 24 hours. Check that yesterday's turn
       now renders as "first turn + count" in today's
       dispatch. Hard to verify without logging the
       full system prompt — this is where live
       validation is mostly "it feels right or it
       doesn't."
- [ ] 13f. `fitt inbox --since 1d --kind system_pruned
       --json` shows history prune ran overnight.

## Definition of done

- All required tasks complete.
- `uv run pytest -q` green across gateway + telegram-bot.
- `tests/e2e/test_session_poisoning_lifecycle.py` passes
  WITHOUT `@pytest.mark.xfail` — flipping strict-xfail
  green means the Phase 5 fix actually landed.
- Roadmap pointer flipped DONE.
- Live validation (13a–13f) green.
- Author has used `fitt learn` in real life at least
  once and the recorded lesson was still present a week
  later.

## Size note

Per the requirements doc: this phase is honestly 1–2
weekends, not "1 weekend." Tool-turn persistence (Task 6)
is a disk-format migration that touches parser +
append + every call site that persists; Decaying
injection (Task 8) adds four new code paths to
`_load_context`. Ship the four thematic groups as
independently committed slices so each commit is a
small, reviewable unit even if the whole phase takes
two sessions.
