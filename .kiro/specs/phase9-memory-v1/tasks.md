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
- [x] 2-5. **Honcho spike — RESOLVED by desk research 2026-07-02:
  reject for v1, go home-grown.** Tasks 2-4 (stand up Honcho,
  implement `HonchoRetrievalProvider`, run the quality bake-off)
  were superseded: primary Honcho docs (server v3.0.9) made the
  cost/fit call without standing it up. Self-host = API + deriver +
  Postgres/pgvector (2-3 services); cloud LLMs by default (fully-
  local is a community-patched path, friction with Principle 5);
  AGPL-3.0; and its reasoning/conclusions value-add is a v1
  non-goal. Decision gate (task 5) outcome: **build the local
  SQLite FTS5 + embeddings provider** (9b). Recorded in requirements
  OD1, design D2, and `docs/observed-issues.md`. Honcho stays a
  documented revisit if v1 grows toward user-model synthesis.

## Phase 9b — Local provider (SQLite FTS5 + embeddings)

- [x] 6. Implement `LocalRetrievalProvider` against the ABC at
  `gateway/src/gateway/retrieval/local.py`: SQLite at
  `$FITT_HOME/memory/index.db`; an FTS5 virtual table for keyword
  search; an embeddings blob column with brute-force cosine in
  Python for semantic search (no compiled `sqlite-vec` — keeps the
  deployment-neutral rule clean at single-user scale). Upsert keyed
  by `(session_id, turn_anchor)`. (Design D2, D4) DONE 2026-07-02:
  index/search (discovery/scroll/browse + semantic/keyword)/reindex/
  status, FTS5 external-content table kept in sync via triggers,
  float32-blob cosine, lineage dedup, scope filter, injected
  `Embedder` protocol (real Ollama embedder is task 7).
- [x] 7. `memory.embedding_alias` resolves through model binding;
  default a local Ollama embed model; dimension-mismatch detection
  in `status()`/`index()` (fail loud). (U7.1, U7.2, P6) DONE
  2026-07-02: `AliasEmbedder` (retrieval/embedder.py) maps an alias's
  `ModelConfig` to the right LiteLLM `aembedding` call (ollama/openai/
  openrouter; anthropic raises), caching dim from the first response.
  `MemoryConfig.embedding_alias` (opt-in; unset = retrieval off) wires
  the `LocalRetrievalProvider` onto `app.state.retrieval_provider` at
  boot — a bad alias degrades to disabled with a loud WARNING rather
  than crashing (retrieval is off the request path). config.example
  documents it. Provider wired but not yet consumed (indexer = 9c).
- [x] 8. Provider contract tests (`test_retrieval_local.py`)
  parametrized over the wired provider (`params=["local"]` — add a
  substrate → add a param): P1, P2, P4, P5, P6, P7, P8, plus the
  semantic/keyword/browse/scroll shapes, via a deterministic
  token-hash fake embedder. (Design testing) DONE 2026-07-02: 11
  tests green.

## Phase 9c — Async indexer (off the hot path)

- [x] 9. `MemoryIndexer` subscribes to turn persistence and calls
  `provider.index(...)` AFTER the response is sent (background
  task / queue). Backend-down → queue/skip + warn; chat
  unaffected. (Design D3, U5.1, U5.3) DONE 2026-07-02:
  `retrieval/indexer.py`. `MemoryStore.append_turn` fires a
  registered listener after the write; `MemoryIndexer.on_turn`
  schedules `loop.create_task(provider.index(...))` and returns
  immediately. `_index_one` swallows backend failures (WARNING;
  re-indexable later). No-provider and no-running-loop are no-ops.
  Wired in create_app (`app.state.memory_indexer` + `set_turn_
  listener`); one hook covers chat + cron persistence.
- [x] 10. Map a persisted turn (incl. Phase-5 structured tool
  turns) → `MemoryDoc` with `{session_id, date, turn_anchor,
  role, text, lineage_root}`. Index Phase-8 compacted summaries
  when present (graceful degradation). (Design D9) DONE 2026-07-02:
  `turn_anchor_from_ts` (matches the on-disk header stamp) +
  `build_turn_text` (user+assistant combined) — both reproducible
  from markdown so a reindex (9f) aligns. role="turn",
  lineage_root=session_id (resume-chain lineage a later refinement).
- [x] 11. Tests: `test_memory_indexer.py` (turn→doc mapping,
  backend-down path) + chat-path isolation: append_turn returns
  without waiting on a (blocked) index. (P3, U5.1, U5.2) DONE
  2026-07-02: 5 tests — markdown-aligned mapping, no-provider/
  no-loop no-ops, the P3 blocked-index-doesn't-block-append_turn
  proof, and listener-failure-doesn't-break-persistence.

## Phase 9d — Retrieval tool

- [x] 12. `memory_search` tool: one tool, three shapes (discovery
  / scroll / browse) + `mode` semantic|keyword + `scope`
  session|all. Read-only ⇒ `auto` bucket; schema follows the
  text-payload-family naming + non-empty description (offline
  lint). Lineage dedup. (Design D5, U2.1–U2.3, U3.1, U6.1) DONE
  2026-07-02: `tools/retrieval_search.py`; provider off
  `ctx.retrieval`; formats hits as `[session date] excerpt`; scope
  defaults to the current session.
- [x] 13. Register via the core registry (`build_core_tool_
  registry`); appears in `list_capabilities`. Tests: schema lint +
  registry membership. DONE 2026-07-02: registered only when
  `memory.embedding_alias` is set (no dead tool on retrieval-off
  deployments); `ctx.retrieval` wired in chat.py (both sites) +
  cron_runner.
- [x] 14. Tests: tool shape, hit formatting, scope pass-through,
  no-provider error, mode validation, conditional registration.
  (P5, P7, U1.1, U1.2) DONE 2026-07-02: `test_retrieval_search_
  tool.py`, 6 tests. (Full HTTP e2e folds into 9g's V-checks.)

## Phase 9e — Prefetch (opt-in, off by default)

- [x] 15. `memory.prefetch_enabled` (default false). When on,
  inject the top excerpt(s) for the current message as a bounded
  `[Recalled context]` block, distinct from `[Learned
  corrections]` + recency history, labeled with provenance.
  (Design D6, U6.2, U6.3) DONE 2026-07-02: `retrieval/prefetch.py`
  (`prefetch_block`, session-scoped, excerpt-capped, provenance-
  labeled, swallows backend errors). `_inject_memory` grows a
  `recalled_block` placed LAST in the system prefix (after
  identity/lessons). `memory.prefetch_enabled`/`prefetch_k` config;
  chat handler calls it only when enabled AND retrieval configured.
- [x] 16. Tests: block present only when enabled + hits exist;
  size-bounded; provenance-labeled; injection ordering.
  (U3.3, P5) DONE 2026-07-02: `test_retrieval_prefetch.py`, 6 tests.

## Phase 9f — Re-index + visibility

- [x] 17. Offline, idempotent re-index script/CLI (`fitt memory
  reindex`) walking `sessions/*/history/*.md` → `provider.
  reindex(...)`; upsert-keyed by `(session, turn_anchor)`. (U4.1,
  U4.2, U8.2, P1, P2) DONE 2026-07-02: `retrieval/reindex.py`
  (`iter_docs_from_markdown` groups blocks by shared timestamp,
  reusing the live turn→doc mapping so anchors align) +
  `fitt memory reindex`.
- [x] 18. Index status on an existing surface: `fitt memory
  status` (doc_count, last_indexed_at, embedding_model, backend
  reachable, dim). (U8.1, U8.2) DONE 2026-07-02. Dashboard card +
  "reindex now" action left as the optional follow-up (task marked
  optional in design); the CLI covers the required visibility.
  `build_retrieval_provider` (retrieval/wiring.py) shared by
  create_app + CLI (removed the duplicated assembly).
- [x] 19. Reindex-equivalence: seed markdown → reindex →
  snapshot results → delete index → reindex → equivalent
  results; idempotent second run. (P1, P2, U4.1) DONE 2026-07-02:
  `test_retrieval_reindex.py`, 4 tests (mapping, equivalence +
  idempotency, missing-dir, provider gating).

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
