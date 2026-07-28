# Phase 9 — Memory v1: Vector / RAG / Cross-Project: Design

## Overview

Phase 9 adds a **retrieval layer** beside the existing
`MemoryStore`. It does not touch decay (Phase 5) or the markdown
ground truth. The new surface is small and pluggable:

- A `RetrievalProvider` ABC — the seam that isolates the OD1
  substrate fork. One interface, two possible implementations.
- A **Honcho-backed provider** as **P0** (the first thing built +
  evaluated), with a **home-grown SQLite FTS5 + embeddings**
  provider retained as the fallback implementation of the same
  ABC.
- An **async indexer** that feeds the provider *after* turn
  persistence — never on the chat hot path.
- A **retrieval tool** (read-only, `auto` bucket) and an optional,
  config-gated, size-bounded **prefetch** injection.
- An **offline, idempotent re-index** script over existing
  markdown.

```
$FITT_HOME/
├── identity/                     (unchanged)
├── sessions/<id>/history/*.md    ground truth (unchanged)
└── memory/                       NEW — derived index, rebuildable
    ├── index.db                  (home-grown fallback: SQLite FTS5 + vectors)
    └── honcho/                   (Honcho self-hosted state, if adopted)
```

Four principles.

1. **Markdown stays the source of truth.** The index is a derived
   artifact. Delete `memory/`, run the re-index, and retrieval
   behaves equivalently. The index never holds data that isn't
   reconstructable from the markdown.
2. **The substrate is behind an ABC.** OD1 (Honcho vs home-grown)
   is a real fork; a `RetrievalProvider` interface means the fork
   lives in one swappable module, the spike can't strand the rest
   of the design, and the tool/indexer/visibility code is written
   once against the interface.
3. **Retrieval is on-demand and off the hot path.** Indexing runs
   after the response is sent; retrieval is pulled when a turn
   benefits from it. The always-injected recency prompt (Phase 5)
   is unchanged in size.
4. **Provenance travels with recalled context.** Every retrieved
   excerpt carries session id + date + anchor, and injected
   recall is labeled as history — reusing Phase 5's anti-poisoning
   discipline so recalled text can't masquerade as current fact.

## Architecture

```
   turn completes + persists (MemoryStore.append_turn)
                     │
                     ▼  (post-response, async — never blocks dispatch)
             MemoryIndexer.sync_turn(session, turn)
                     │
                     ▼
           RetrievalProvider.index(doc)        ◄── embedding_alias
        ┌─────────────────────────────┐            (Ollama, config)
        │ Honcho provider (P0)         │
        │   — external service         │
        │ OR                           │
        │ Local provider (fallback)    │
        │   — SQLite FTS5 + vectors    │
        └─────────────────────────────┘
                     ▲
                     │  query (on demand)
      ┌──────────────┴───────────────┐
      ▼                              ▼
  memory_search tool            prefetch (optional, config-gated)
  (registry, auto bucket)       injected as a labeled, bounded
   discovery / scroll /          [Recalled context] block before
   browse + semantic             the current user message
```

Two entry points, one provider:

- **Write path:** `MemoryIndexer` subscribes to turn persistence
  and calls `provider.index(...)` asynchronously. Down backend →
  queue/skip + warn; chat unaffected (U5).
- **Read path:** the `memory_search` tool and the optional
  prefetch both call `provider.search(...)`. Same provider, same
  results shape.

## The `RetrievalProvider` ABC

The one interface both substrates implement. Modeled on Honcho's
`MemoryProvider` contract (via the Hermes plugin) so a Honcho
adoption is a thin wrapper, but trimmed to Phase 9's v1 scope
(no `conclude`/user-model synthesis — a non-goal).

```python
class RetrievalProvider(ABC):
    async def index(self, doc: MemoryDoc) -> None: ...
    async def search(self, q: RetrievalQuery) -> list[RetrievalHit]: ...
    async def reindex(self, docs: Iterable[MemoryDoc]) -> IndexStats: ...
    async def status(self) -> IndexStatus: ...
```

- `MemoryDoc`: `{session_id, date, turn_anchor, role, text,
  lineage_root}` — derived from a persisted turn.
- `RetrievalQuery`: `{text, mode: semantic|keyword, scope:
  session|all, session_id, k, anchor?}` — the three shapes
  (discovery / scroll / browse) fall out of which fields are set,
  Hermes-style, rather than three tools (U2.2).
- `RetrievalHit`: `{session_id, date, turn_anchor, excerpt,
  score, lineage_root}` — an anchored window (U1.2), always with
  locator metadata (U1.1) and provenance (U6.3).
- `IndexStatus`: `{doc_count, last_indexed_at, embedding_model,
  backend_reachable, dim}` — feeds U8 visibility and U7.2
  dimension-mismatch detection.

Contract tests (below) run against *whichever* provider is wired,
so the fallback is never a second-class citizen.

## Design decisions

- **D1. Substrate behind an ABC (resolves OD1's structural
  risk).** The Honcho-vs-home-grown choice is contained to one
  module implementing `RetrievalProvider`. Everything else
  (indexer, tool, prefetch, visibility, tests) is substrate-
  agnostic. Rationale: the spike can decide late without
  reworking the phase.
- **D2. Substrate: home-grown local provider (RESOLVED
  2026-07-02).** The Honcho spike (P0) was resolved by desk
  research rather than a live bake-off: Honcho self-hosts as API +
  deriver + Postgres/pgvector, defaults to cloud LLMs (friction
  with Principle 5), is AGPL-3.0, and its reasoning/conclusions
  value-add is a v1 non-goal. For an always-on local single-user
  hub wanting search over its own markdown, that's too much
  surface for too little v1 value. **Decision: build the local
  `SQLite FTS5 + embeddings` provider against the ABC.** Honcho
  stays a documented revisit if v1 later grows toward user-model
  synthesis. The ABC (already landed) means this is the only module
  the decision changes.
  - **Local provider shape:** SQLite for storage; an FTS5 virtual
    table for keyword search; embeddings stored as a blob column
    with brute-force cosine similarity in Python for semantic
    search. Brute-force is deliberate — at single-user corpus scale
    (thousands of turns) it's fast enough and avoids a compiled
    vector extension (`sqlite-vec`), keeping the deployment-neutral
    rule clean (no native build step). Revisit an ANN index only if
    corpus size ever makes linear scan bite.
- **D3. Indexing is async, post-persistence.** `MemoryIndexer`
  runs after `append_turn`, off the request path (a background
  task / queue). A dropped or delayed index entry is acceptable
  (U5.2); a blocked chat turn is not. Backend-down → log +
  catch-up later (U5.3).
- **D4. Markdown is ground truth; index is a rebuildable
  derivative.** The re-index script is offline and idempotent
  (U4.1, U4.2): keyed by `(session_id, turn_anchor)` so re-runs
  upsert rather than duplicate. Deleted markdown → its entries
  don't surface as live hits after the next pass (U4.3).
- **D5. One tool, three shapes + two modes.** `memory_search`
  exposes discovery (query → ranked sessions w/ anchored window),
  scroll (session + anchor → neighbors), browse (no args → recent),
  and a `mode` of semantic|keyword — no tool proliferation (U2.2).
  Read-only ⇒ `auto` bucket (U6.1). Lineage dedup so a resumed
  thread isn't returned N times (U2.3).
- **D6. Prefetch is opt-in, off by default.** A config flag
  (`memory.prefetch_enabled`, default false) gates auto-injecting
  the top excerpt(s) for the current message into a bounded
  `[Recalled context]` block, distinct from `[Learned corrections]`
  and the recency history, labeled with provenance (U6.2, U6.3).
  Stays off until the spike shows it helps more than it costs
  (token budget vs relevance).
- **D7. Embedding model is an alias (config, not code).**
  `memory.embedding_alias` resolves through existing model
  binding, default a local Ollama embed model (OD3, chosen in the
  spike). A stored-index dimension that disagrees with the
  configured model is detected at status/index time and reported
  loudly (Principle 11), with the fix being a re-index (U7.2).
- **D8. Cross-session is opt-in per query.** `scope` defaults to
  the current session; `scope=all` opts into cross-session, and
  hits carry their originating session (U3.1, U3.2). The
  default-injected prompt is untouched (U3.3).
- **D9. Graceful degradation vs Phase 8 (resolves OD2).** The
  indexer indexes whatever is on disk — Phase-5 structured history
  now, Phase-8 compacted summaries when they exist. No hard gate
  on compaction; retrieval quality improves when it lands. Phase 5
  already stores tool turns compactly, so verbatim-noise is
  bounded today.

## Correctness properties

Numbered so tests can cite them (repo convention).

- **P1. Index is derivable.** For any markdown corpus, delete the
  index + `reindex()` ⇒ retrieval results equivalent to before
  the delete. (U4.1)
- **P2. Reindex is idempotent.** `reindex()` run twice ⇒ same
  `doc_count` and same results; no duplicate `(session,anchor)`
  entries. (U4.2)
- **P3. Chat path is embedding-free.** A chat turn issues zero
  embedding/index dispatches on the request-response path;
  indexing dispatches happen only after the response. (U5.1)
- **P4. Empty/cold index never raises.** `search()` on a fresh
  FITT returns `[]`. (U1.3)
- **P5. Provenance is preserved.** Every `RetrievalHit` and every
  injected recall block carries `{session_id, date, turn_anchor}`.
  (U1.1, U6.3)
- **P6. Dimension mismatch is loud.** Configured embedding dim ≠
  stored index dim ⇒ `status()` reports it and index/search
  surfaces a clear error, never silent wrong-space results. (U7.2)
- **P7. Scope defaults to current session.** `search()` without
  `scope=all` never returns other sessions' hits. (U3.1)
- **P8. Lineage dedup.** A resumed conversation appears once in
  discovery results, keyed by `lineage_root`. (U2.3)

## Testing strategy

- **Provider contract tests** (`test_retrieval_provider_contract.py`):
  a shared test body parametrized over the wired provider(s),
  asserting P1/P2/P4/P5/P6/P7/P8. The fallback provider must pass
  the same suite as Honcho's wrapper — that's what keeps it real.
- **Indexer unit** (`test_memory_indexer.py`): sync_turn maps a
  persisted turn to a `MemoryDoc`; backend-down queues/skips +
  warns (P3-adjacent, U5.3).
- **Chat-path isolation** (e2e): a chat turn through the HTTP
  pipeline issues no embedding dispatch pre-response (P3). Extends
  the Phase 4.6 harness (stubbed LLM + stub embedding backend that
  records calls).
- **Reindex equivalence** (e2e/integration): seed markdown → index
  → snapshot results → delete `memory/` → reindex → assert
  equivalent (P1, P2).
- **Retrieval tool** (e2e): `memory_search` via the HTTP tool path
  returns anchored excerpts with provenance; `scope=all` opt-in
  behaves (P5, P7).
- **Quality bar (spike, U1.4):** on a real/synthetic multi-week
  corpus, a "remember when we discussed X" query retrieves the
  relevant 2-week-old turn that recency-injection alone drops.
  Recorded in the spike outcome, not left to "feels right."

## Spike plan (Phase 9a) — Honcho as P0

Time-boxed (~1–2 days) per the roadmap. Deliverable: a
`HonchoRetrievalProvider` behind the ABC + a written decision.

1. Stand up Honcho self-hosted (compose service alongside the
   gateway; state under `FITT_HOME/memory/honcho/`).
2. Implement `HonchoRetrievalProvider` (index via `sync_turn`,
   search via Honcho `search`, status).
3. Point it at a real session export; run the U1.4 quality query
   set.
4. **Decide** against explicit criteria: (a) retrieval quality
   clears U1.4; (b) deployment-neutral fit (no code container
   branch, compose-only glue, `FITT_HOME` state, offline-friendly);
   (c) operational weight acceptable for an always-on single-user
   hub. All three yes → adopt. Any hard no → build the local
   provider against the same ABC (design carries both; only the
   wired module changes).
5. Record the outcome in the spec + `docs/observed-issues.md`.

## Deployment-neutrality notes

- Index + any service state live under `FITT_HOME/memory/`; no
  `if container:` branch. Compose glue (a Honcho service, the
  embedding model host) lives in the compose file + `.env`, not in
  Python (per the deployment-neutral rule).
- Embedding + (if adopted) Honcho endpoints are config/aliases,
  resolved identically native or containerized.

## References

- `docs/prior-art.md` — Honcho five-tool surface + Hermes plugin
  `sync_turn`/`prefetch` contract; FTS5 anchored-window
  three-shape `session_search_tool.py`.
- Phase 5 design (`.kiro/specs/phase5-lessons/design.md`) — the
  markdown-first, permissive-parser, provenance discipline this
  builds on.
