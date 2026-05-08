# Phase 5 — Lessons + Decaying History: Tasks

Status legend: `[x]` done, `[ ]` not yet.

## 1. Spec promotion

- [ ] 1a. Promote Phase 5 from `FITT_ROADMAP.md` inline
       draft to the three-file spec here:
       `requirements.md`, `design.md`, `tasks.md`.
- [ ] 1b. Commit the spec as its own change before any
       code lands, matching the Phase 4.5 / 4.6 / 4.7
       convention.

## 2. LessonsStore (plumbing only, no wiring yet)

- [ ] 2a. `gateway/src/gateway/lessons.py`: `Lesson`
       dataclass (`text`, `category: str | None`,
       `added_ts: float`), `LessonsStore` with `read()`,
       `add()`, `remove(substring)`, `render_block()`.
- [ ] 2b. Persistence at `$FITT_HOME/identity/lessons.md`
       with the template scaffolding + an "Active lessons"
       section the store parses and mutates.
       Write-through mutations (read → parse → mutate →
       write) under a thread lock, matching CronService.
- [ ] 2c. `max_entries` (default 50) with oldest-dropped
       behaviour on overflow.
- [ ] 2d. Unit tests: round-trip add/list/remove, max
       rollover, mtime-based freshness, malformed file
       degrades to empty + warning.

## 3. Lessons injection into the system prompt

- [ ] 3a. `MemoryStore._load_lessons()` reads the
       lessons file and returns the rendered block (or
       empty string when no store / disabled).
- [ ] 3b. `load_context()` composes
       `identity + [Learned corrections] block +
       [Past activity] summary` into `system_prefix`. Order
       documented inline so a reader can see the three
       layers stack.
- [ ] 3c. Unit tests: empty lessons → empty block;
       lessons present → block rendered in the right
       spot; identity unchanged when lessons file is
       missing.

## 4. `learn_*` inline tools

- [ ] 4a. `gateway/src/gateway/tools/lessons.py`:
       `learn_add`, `learn_list`, `learn_remove`.
       Default buckets: `learn_list` = `auto`;
       `learn_add` / `learn_remove` = `ask`.
- [ ] 4b. Register in the tool registry via
       `build_lessons_tools()` mirroring
       `build_cron_tools`.
- [ ] 4c. `ToolContext` grows a `lessons: Any = None`
       field. Wire in `chat.py` and `cron_runner.py`
       alongside the existing `cron` / `events`
       wiring.
- [ ] 4d. Unit tests per tool.

## 5. `fitt learn` CLI

- [ ] 5a. Group `@main.group("learn")` mirroring
       `fitt cron`.
- [ ] 5b. `fitt learn list`, `fitt learn add "text"
       [--category X]`, `fitt learn remove <substring>`,
       `fitt learn path`.
- [ ] 5c. Tests (CLI round-trips to LessonsStore).

## 6. Tool-turn persistence format

- [ ] 6a. Extend `_HEADER_RE` in `memory.py` to match
       `user` / `assistant` / `assistant tool_calls` /
       `tool <name>` / `system`.
- [ ] 6b. `PersistedToolCall` dataclass in `memory.py`
       (`tool_name`, `args_summary`, `result_status`,
       `result_summary`).
- [ ] 6c. `append_turn` grows `tool_calls: list[
       PersistedToolCall] | None = None` kwarg. When
       present, write the three extra sub-blocks
       (`assistant tool_calls`, `tool <name>` per call,
       final `assistant`).
- [ ] 6d. `_parse_turns` handles the new headers.
       Unknown-header turns drop with a debug log, not
       a raise (back-compat with future schemas).
- [ ] 6e. Loading a tool-using turn produces an
       OpenAI-shape message sequence:
       `[user, assistant+tool_calls, tool, assistant]`.
       Generate deterministic `tool_call_id` from args
       hash so the tool-role entry pairs correctly.
- [ ] 6f. Unit tests: round-trip a tool-using turn,
       load pre-Phase-5 chat-only file identically
       (back-compat), unknown-header degradation.

## 7. Wire `PersistedToolCall` collection in call sites

- [ ] 7a. `agent_loop.py`: `AgentLoopResult` grows a
       `tool_calls_for_memory: list[PersistedToolCall]`
       field. The loop accumulates one per call.
- [ ] 7b. `chat.py`: after `run_agent_loop`, pass
       `result.tool_calls_for_memory` to
       `memory.append_turn`.
- [ ] 7c. `cron_runner.py`: same pattern.
- [ ] 7d. `detach.py`: detached worker collects the
       calls too; passes to `append_turn` at the end.
- [ ] 7e. E2E lifecycle check: existing lifecycle tests
       still pass (pinning that the change didn't
       regress the HTTP/approval flow).

## 8. Decaying history injection

- [ ] 8a. `_load_decaying_history` replaces
       `_load_and_truncate_history`. Assembles today's
       full turns + yesterday's first + count + 3–30
       day one-line summaries.
- [ ] 8b. Summary text is deterministic:
       `"YYYY-MM-DD: N user turns (with tools|chat
       only)"`.
- [ ] 8c. Layered budget: oldest layer drops first when
       total exceeds `max_history_chars`.
- [ ] 8d. Summaries render as a single `system`
       message with `[Past activity]` header, prepended
       to the turn list.
- [ ] 8e. Unit tests per layer + budget-truncation
       ordering.

## 9. History pruner

- [ ] 9a. `gateway/src/gateway/history_pruner.py`:
       `HistoryPruner` mirroring the shape of
       `EventPruner`.
- [ ] 9b. `tick()` walks
       `$FITT_HOME/sessions/*/history/*.md`, parses
       each filename's date, drops files past
       `memory.history_max_days`.
- [ ] 9c. Emits `system_pruned` with
       `meta.target="history"` and `meta.removed=<n>`.
- [ ] 9d. Anchor file for restart-safe cadence (same
       pattern as the event pruner).
- [ ] 9e. Wire start/stop in `create_app`.
- [ ] 9f. Unit tests.

## 10. E2E + regression

- [ ] 10a. `tests/e2e/test_lessons_lifecycle.py`:
       stubbed LLM emits `learn_add("always use
       uv")`; approver approves; next request dispatches
       with `[Learned corrections]` block containing the
       lesson. Assert via `stubbed_llm.calls[-1]`.
- [ ] 10b. Flip
       `tests/e2e/test_session_poisoning_lifecycle.py`
       off xfail. Its existing assertion ("stale 'SSH
       unreachable' refusal NOT in next dispatch") must
       now pass unassisted. If it fails, the tool-turn
       persistence didn't land completely — fix
       before declaring done.

## 11. Config + docs

- [ ] 11a. Add `memory.max_lessons`,
       `memory.history_max_days`, and the optional
       `memory.history_decay` block to `Config`.
- [ ] 11b. Update `configs/config.example.yaml` with
       the new fields commented.
- [ ] 11c. Brief notes in `docs/` (or a new
       `docs/memory.md`) explaining the four layers
       and the decay shape.

## 12. Roadmap pointer update

- [ ] 12a. Flip the Phase 5 inline draft's "to be
       written" footnote to "Spec promoted
       YYYY-MM-DD; implementation in
       `.kiro/specs/phase5-lessons/`."
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
