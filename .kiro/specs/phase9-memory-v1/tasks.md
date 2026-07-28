# Implementation Plan: FITT Phase 9 — Memory v1 (Vector / RAG / Cross-Project)

**Status:** not started

## Overview

Add an on-demand retrieval layer beside `MemoryStore`, behind a
`RetrievalProvider` ABC so the OD1 substrate fork stays contained.
Build + evaluate a Honcho-backed provider first (P0 spike); if it
doesn't fit, implement a local SQLite FTS5 + embeddings provider
against the same ABC. Then the substrate-agnostic pieces: async
indexer, retrieval tool, optional prefetch, offline re-index, and
operator visibility. Markdown stays ground truth; indexing never
blocks the chat path; each slice keeps the tree green.

Status legend: `[x]` done, `[ ]` not yet.

## Phase 9a — Honcho spike (P0, decision gate)

- [x] 1. Define the `RetrievalProvider` ABC + dataclasses
  (`MemoryDoc`, `RetrievalQuery`, `RetrievalHit`, `IndexStats`,
  `IndexStatus`) in `gateway/src/gateway/retrieval/base.py`. Pure
  types + ABC; no substrate yet. (Design D1) DONE 2026-07-02:
  frozen-slots value types + async ABC (index/search/reindex/
  status + `name`); `RetrievalMode`/`RetrievalScope` literals;
  `test_retrieval_base.py` covers construction/frozen, ABC
  non-instantiability, and a minimal concrete provider. The
  behavioral contract suite (Properties 1-8) lands in 9b.
- [ ] 2. Stand up Honcho self-hosted as a compose service; state
  under `$FITT_HOME/memory/honcho/`; endpoint via config/alias.
  Compose + `.env` glue only — no Python container branch.
  (Design deployment notes, U4.4)
- [ ] 3. Implement `HonchoRetrievalProvider` against the ABC
  (`index` via sync_turn, `search`, `status`). (Design D2)
- [ ] 4. Run the U1.4 quality query set on a real/synthetic
  multi-week corpus; record retrieval quality. (U1.4)
- [ ] 5. **Decision gate.** Evaluate against D2's three criteria
  (quality clears U1.4; deployment-neutral fit; operational
  weight). Record the outcome in this spec + `docs/observed-
  issues.md`. If adopt → 9b wires Honcho. If reject → 9b adds the
  local provider against the same ABC.

## Phase 9b — Substrate wiring (Honcho adopted) OR local provider

- [ ] 6. (adopt path) Wire `HonchoRetrievalProvider` into
  `app.state`; config keys (`memory.retrieval_backend`,
  endpoints) with `.example` templates. OR (fallback path)
  implement `LocalRetrievalProvider` (SQLite FTS5 + a vector
  column/table; `$FITT_HOME/memory/index.db`) against the ABC.
  (Design D2, D7)
- [ ] 7. `memory.embedding_alias` resolves through model binding;
  default a local Ollama embed model; dimension-mismatch detection
  in `status()`/`index()` (fail loud). (U7.1, U7.2, P6)
- [ ] 8. Provider contract tests (`test_retrieval_provider_
  contract.py`) parametrized over the wired provider: P1, P2, P4,
  P5, P6, P7, P8. (Design testing)

## Phase 9c — Async indexer (off the hot path)

- [ ] 9. `MemoryIndexer` subscribes to turn persistence and calls
  `provider.index(...)` AFTER the response is sent (background
  task / queue). Backend-down → queue/skip + warn; chat
  unaffected. (Design D3, U5.1, U5.3)
- [ ] 10. Map a persisted turn (incl. Phase-5 structured tool
  turns) → `MemoryDoc` with `{session_id, date, turn_anchor,
  role, text, lineage_root}`. Index Phase-8 compacted summaries
  when present (graceful degradation). (Design D9)
- [ ] 11. Tests: `test_memory_indexer.py` (turn→doc mapping,
  backend-down path) + e2e chat-path isolation: a turn issues zero
  embedding dispatch pre-response. (P3, U5.1, U5.2)

## Phase 9d — Retrieval tool

- [ ] 12. `memory_search` tool: one tool, three shapes (discovery
  / scroll / browse) + `mode` semantic|keyword + `scope`
  session|all. Read-only ⇒ `auto` bucket; schema follows the
  text-payload-family naming + non-empty description (offline
  lint). Lineage dedup. (Design D5, U2.1–U2.3, U3.1, U6.1)
- [ ] 13. Register via the core registry (`build_core_tool_
  registry`); appears in `list_capabilities`. Tests: schema lint +
  registry membership.
- [ ] 14. E2E: `memory_search` via the HTTP tool path returns
  anchored excerpts with provenance; `scope=all` opt-in behaves.
  (P5, P7, U1.1, U1.2)

## Phase 9e — Prefetch (opt-in, off by default)

- [ ] 15. `memory.prefetch_enabled` (default false). When on,
  inject the top excerpt(s) for the current message as a bounded
  `[Recalled context]` block, distinct from `[Learned
  corrections]` + recency history, labeled with provenance.
  (Design D6, U6.2, U6.3)
- [ ] 16. Tests: block present only when enabled + hits exist;
  size-bounded; provenance-labeled; default-injected prompt
  unchanged when off (U3.3, P5).

## Phase 9f — Re-index + visibility

- [ ] 17. Offline, idempotent re-index script/CLI (`fitt memory
  reindex`) walking `sessions/*/history/*.md` → `provider.
  reindex(...)`; upsert-keyed by `(session, turn_anchor)`. (U4.1,
  U4.2, U8.2, P1, P2)
- [ ] 18. Index status on an existing surface: `fitt memory
  status` (doc_count, last_indexed_at, embedding_model, backend
  reachable, dim) + optional dashboard card + "reindex now"
  action. (U8.1, U8.2)
- [ ] 19. E2E reindex-equivalence: seed markdown → index →
  snapshot results → delete `memory/` → reindex → equivalent
  results. (P1, P2, U4.1)

## Phase 9g — Close-out

- [ ] 20. Full `uv run pytest -q` green across gateway +
  telegram-bot; ruff + mypy clean both packages.
- [ ] 21. Roadmap Phase 9 pointer → DONE with date; BACKLOG
  Now/Next updated; observed-issues carries the spike outcome +
  any retrieval-quality findings.

## Verification (manual, on the hub)

- [ ] V1. Ask a "remember when we discussed X" turn that predates
  the recency window; confirm the agent (via `memory_search` or
  prefetch) surfaces the relevant older turn.
- [ ] V2. `scope=all` query returns hits from another session,
  labeled by session; a default-scope query does not.
- [ ] V3. Edit/delete a history file, `fitt memory reindex`,
  confirm retrieval reflects the change (no dead/fabricated hits).
- [ ] V4. Kill the embedding backend; confirm chat still responds
  at normal latency and indexing catches up when it returns.

## Definition of done

- All requirements' acceptance criteria green; properties P1–P8
  covered by tests.
- OD1 resolved (spike outcome recorded); OD2/OD3 settled in
  design/config.
- Markdown authoritative; reindex-equivalence + chat-path-
  isolation tests green.
- Deployment-neutral (no container branch; `FITT_HOME`-rooted).
- Standard test/lint/typecheck cycle green in both packages.

## Notes

- **Spike-first (9a) is load-bearing.** The ABC lands regardless;
  only the wired provider module changes on the decision. Don't
  build 9c–9f against Honcho specifics — code to the ABC.
- **Not a MemoryStore rewrite.** Retrieval is additive and
  on-demand; decay (Phase 5) still owns the always-injected
  recency window.
- **Quality is pinned, not vibed.** U1.4 is the concrete gate;
  avoid repeating the Phase 5 "live validation, feels right"
  trap — record the query set + outcome.
