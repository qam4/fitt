# Phase 9 — Memory v1: Vector / RAG / Cross-Project: Requirements

## Context

Through Phase 5, FITT's memory is markdown-first and
**recency-scoped**: identity + lessons are always injected;
session history decays by age (today verbatim, yesterday
collapsed, 3–30 days as one-line markers, 30+ dropped from the
prompt). This is deliberately simple and works for daily
continuity. Two gaps remain, and they're the ones that make the
assistant feel forgetful:

1. **No recall of older-but-relevant context.** "Remember when
   we discussed the training-run flakiness two weeks ago" fails —
   that day decayed to a one-line marker (or dropped) in the
   injection path, even though the full turn is still on disk. The
   information exists; nothing retrieves it by relevance.
2. **No cross-session / cross-project recall.** Sessions are
   isolated by design (`$FITT_HOME/sessions/<id>/`). "What did I
   learn about deployment last month, in any project" can't be
   answered — each session only ever reads its own history.

Phase 9 adds a **retrieval layer on top of the existing
markdown**: keyword + semantic search across sessions, an
opt-in cross-session scope, and bounded injection of the most
relevant prior context into a turn. Markdown stays the ground
truth; the index is derived and rebuildable.

This is the "assistant-shape" feature the project-overview
steering calls high-payoff — the thing that turns FITT from
"remembers today" into "gets to know you over time."

## Relationship to the existing memory layers

Phase 9 does **not** replace decay (Phase 5) or compaction
(Phase 8). They compose:

- **Decay (Phase 5)** governs what's injected *by default* for the
  *current* session — the always-there recency window.
- **Compaction (Phase 8)** rewrites long *in-session* history into
  a rolling summary. Phase 9 indexes whatever compaction leaves on
  disk; where compaction hasn't run, it indexes the Phase-5
  structured history (already compact: short tool results, hoisted
  large outputs).
- **Retrieval (Phase 9)** is *on-demand* and *cross-session*:
  pulled in when a query benefits from older or other-session
  context, not injected every turn.

The markdown files remain authoritative. The index is a derived
artifact that can be deleted and rebuilt from the markdown at any
time.

## Scope

A new retrieval subsystem alongside `MemoryStore` — not a
rewrite of it. Concretely:

- An index over session history (and lessons) supporting keyword
  and semantic search.
- A background indexer that updates the index *after* turn
  persistence (never on the chat hot path).
- A retrieval tool the model can call, plus bounded prefetch
  injection.
- An offline, idempotent re-index script for existing history.
- Operator visibility (index size, freshness) on an existing
  surface (dashboard / CLI).

Substrate choice (external service vs home-grown) is an **open
decision** resolved in design (see below), gated on a short
evaluation spike per the roadmap. The requirements here are
written substrate-agnostic on purpose: they state *what recall
must do*, not *which store provides it*.

## Open decisions (resolved in design.md)

These are called out here so the requirements stay honest about
what isn't yet settled. Each is an architecture-level choice that
warrants an explicit decision with rationale in design.md.

- **OD1. Substrate.** Adopt **Honcho** (plastic-labs, MIT;
  external cross-session user-modeling service, hosted or
  self-hosted) via a FITT-side plugin, OR build **home-grown
  SQLite FTS5 + embeddings** (Hermes `session_search_tool.py`
  reference). Tension: Honcho is more capable out of the box
  (Principle 3, use mature tools) but adds a service + its own
  model to the deployment surface (friction with the
  deployment-neutral rule and Principle 5, local/no-subscription).
  Resolution: a time-boxed Honcho spike (Phase 9a) decides;
  requirements below hold either way.
  **RESOLVED 2026-07-02 → home-grown.** The Phase 9a spike was
  short-circuited by desk research (primary Honcho docs, server
  v3.0.9): self-hosting is API + deriver worker + Postgres/pgvector
  (2-3 services), defaults to cloud LLMs (Gemini/Anthropic/OpenAI —
  fully-local is a non-default community-patched path, friction with
  Principle 5), is AGPL-3.0, and its value-add (reasoning /
  conclusions / peer-modeling) is exactly what v1 scoped out (see
  non-goals). FITT-v1 wants keyword + vector search over its own
  markdown, which the home-grown provider does directly and
  deployment-neutral. Not an empirical quality bake-off — a cost/fit
  call. Revisit Honcho if requirements later grow toward user-model
  synthesis.
- **OD2. Phase 8 coupling.** Proceed now with graceful
  degradation (index existing Phase-5 structured history; quality
  improves when compaction lands) rather than hard-gating on
  Phase 8. Rationale: Phase 5 already stores tool turns compactly
  on disk, so the "noisy verbatim tool output" concern the roadmap
  cited is largely already mitigated.
- **OD3. Embedding model.** Which local Ollama embedding model
  (`nomic-embed-text`, `all-minilm`, …). Per the roadmap this is a
  config knob, not architecture — bound to an alias like every
  other model (Principle 7). The default is chosen during the
  spike by retrieval-quality spot-check.

## User stories

### U1. Semantic recall across sessions

As a FITT user, I want to ask "remember when we discussed X"
and get the relevant prior turns back, even if they're weeks old
or in another session, so the assistant stops feeling amnesiac.

**Acceptance:**

- **1.1** A retrieval call with a natural-language query returns
  the top-k most relevant prior excerpts ranked by semantic
  similarity, each carrying enough locator metadata (session id,
  date, turn anchor) to be traceable back to the markdown.
- **1.2** Results are *excerpts with context* (an anchored window
  around the hit), not whole-session dumps, so the injected
  payload stays bounded.
- **1.3** Retrieval never raises on a cold/empty index — an
  un-indexed FITT returns "no results," not an error.
- **1.4** Relevance is demonstrably better than recency alone: a
  spike/eval shows a 2-week-old relevant turn is retrievable for a
  representative "remember when" query that decay-injection alone
  would miss.

### U2. Keyword / exact-phrase search

As a FITT user, I want to find the exact phrase or identifier I
used before ("the pid 456 monitoring job", an error string), so
I can locate specifics that semantic search fuzzes over.

**Acceptance:**

- **2.1** A keyword search returns matches for exact tokens /
  phrases (FTS-style), complementary to semantic search.
- **2.2** The two retrieval modes are available through one
  coherent tool surface (a small set of shapes), not two
  unrelated tools — following the Hermes discovery/scroll/browse
  three-shape pattern rather than proliferating tools.
- **2.3** Keyword search honors session lineage: a resumed /
  continued conversation isn't returned N times for one logical
  thread.

### U3. Cross-project / cross-session scope is opt-in per query

As a FITT user, I want single-session reads to stay fast and
isolated by default, but be able to say "search across all
sessions," so cross-project recall is available without
polluting every turn.

**Acceptance:**

- **3.1** Default retrieval scope is the current session; a query
  can opt into cross-session scope explicitly (a parameter /
  shape), and results carry which session they came from.
- **3.2** Cross-session results are scoped/labeled by session
  metadata so the model (and operator) can tell provenance.
- **3.3** The always-injected default prompt is unchanged in size
  by this phase — cross-session recall is pulled on demand, not
  pushed every turn.

### U4. Markdown stays ground truth; the index is derived

As a FITT maintainer, I want the vector/keyword index to be a
rebuildable derivative of the markdown, so "shareable by
construction" and hand-editability survive.

**Acceptance:**

- **4.1** Deleting the entire index and re-running the re-index
  script reproduces equivalent retrieval behavior from the
  markdown alone (the index holds no ground-truth-only data).
- **4.2** The re-index script is **offline** (not on the request
  path) and **idempotent** (running twice doesn't duplicate
  entries or change results).
- **4.3** Hand-editing or deleting a markdown history file is
  eventually reflected in retrieval after the next index pass;
  stale index entries for deleted content don't surface as live
  results (or are clearly dead-linked, never fabricated).
- **4.4** Index storage location derives from `FITT_HOME` (env,
  default `~/.fitt`) and resolves identically native or in a
  container — no `if container:` branch (deployment-neutral rule).

### U5. Indexing never blocks the chat path

As a FITT operator, I want turns to persist and respond at the
same latency as today, with indexing happening after the fact,
so recall never costs interactive speed.

**Acceptance:**

- **5.1** Embedding / indexing of a turn happens **after** turn
  persistence completes, off the request-response path (async
  background task or post-response hook).
- **5.2** A small retrieval-freshness lag (the just-finished turn
  may not be retrievable for a moment) is acceptable and
  documented; blocking the chat path on embeddings is not.
- **5.3** If the embedding backend (Ollama satellite) is down,
  chat is unaffected; indexing degrades (queues or skips with a
  logged warning) and catches up when the backend returns.

### U6. Retrieval is available to the model and (bounded) to the prompt

As a FITT user, I want the agent to reach for prior context when
a turn benefits from it, without me manually running a search,
so recall feels automatic.

**Acceptance:**

- **6.1** A retrieval **tool** exists in the registry (approval
  bucket: `auto` — read-only search), so the model can search
  when it recognizes a "remember when" turn. It follows the
  tool-schema conventions (text-payload family naming, non-empty
  description) the offline lint checks.
- **6.2** Optional **prefetch**: the most relevant prior excerpt(s)
  for the current user message may be injected into the system
  prompt as a clearly-labeled, size-bounded block (distinct from
  `[Learned corrections]` and the recency history). Prefetch is
  config-gated and off by default until the spike shows it helps
  more than it costs.
- **6.3** Injected retrieval context is labeled with provenance
  (session + date) so the model treats it as recalled history, not
  current-turn fact — reusing the anti-poisoning discipline from
  Phase 5.

### U7. Embedding model is configuration, not architecture

As a FITT maintainer, I want the embedding model bound to an
alias in config, so swapping it is a config change with no code
edit (Principle 7).

**Acceptance:**

- **7.1** The embedding model is named via an alias / config key
  (e.g. `memory.embedding_alias`), resolved through the existing
  model-binding machinery, defaulting to a local Ollama embedding
  model.
- **7.2** Changing the embedding model is a config change; the
  only code-side consequence is that the index must be rebuilt
  (dimensions/space changed), which the re-index script handles
  and which is surfaced as an operator note, not a silent
  mismatch (Principle 11, fail loud on detectable
  misconfiguration — e.g. a dimension mismatch between the stored
  index and the configured model is detected and reported).

### U8. Operator visibility into the index

As a FITT operator, I want to see how much is indexed and how
fresh it is, so I can tell whether recall is working.

**Acceptance:**

- **8.1** Index status (documents/turns indexed, last index time,
  configured embedding model, backend reachable?) is visible on an
  existing surface (dashboard card and/or a `fitt` CLI command) —
  no new bespoke UI.
- **8.2** A manual "reindex now" trigger exists (CLI at minimum;
  dashboard action optional) so an operator can force a rebuild
  after editing history or swapping the embedding model.

## Definition of done

- All user stories' acceptance criteria green.
- OD1/OD2/OD3 resolved in design.md with rationale (spike
  outcome recorded).
- `uv run pytest -q` green across gateway + telegram-bot.
- Markdown remains authoritative: an end-to-end test proves
  delete-index → reindex → equivalent retrieval (U4.1).
- Chat-path latency unaffected: a test proves indexing is
  off the request path (U5.1).
- Deployment-neutral: no container branch; index path from
  `FITT_HOME` (U4.4).
- Roadmap pointer for Phase 9 flipped to DONE with validation
  date; BACKLOG/observed-issues updated per convention.

## Non-goals (deferred)

- **Automatic user-model synthesis / "conclusions."** Honcho's
  `conclude`-style persistent inferred facts about the user are
  out of scope for v1 — retrieval over what was actually said,
  not LLM-inferred profiles. (Revisit if the spike shows Honcho
  proper and it comes for free.)
- **Cross-user separation.** Single-user FITT; not a memory
  problem here.
- **Real-time / per-turn embedding on the hot path.** Explicitly
  async (U5).
- **Automatic summary regeneration.** Compaction (Phase 8) owns
  in-session summarization; Phase 9 reads what it wrote.
- **Knowledge-graph substrate** (Beever Atlas shape, prior-art) —
  deferred until multi-surface data justifies a graph.
- **Editing indexed content through a UI.** The markdown is the
  edit surface; the index follows.

## Risk / size note

The roadmap scoped this at "~3 weekends," and that's honest —
it's the biggest single arc since the gateway. The risk isn't the
plumbing (an FTS5 + embeddings index is well-trodden); it's
(a) the substrate decision (OD1) genuinely forking the design, so
the Phase 9a spike must be time-boxed and decisive, and (b)
retrieval *quality* being subjective — hence U1.4 pins a concrete
"a 2-week-old relevant turn is retrievable" bar rather than
leaving "it feels right" as the gate (the same trap the Phase 5
live-validation section fell into). Ship as independently
committed slices: spike → index + indexer → retrieval tool →
prefetch → visibility, each leaving the tree green.

## References

- `docs/prior-art.md` — Hermes audit: Honcho integration shape
  (five-tool `profile/search/reasoning/context/conclude` surface,
  `sync_turn`/`prefetch` contract) and the FTS5 anchored-window
  `session_search_tool.py` three-shape (discovery/scroll/browse)
  pattern. Beever Atlas = deferred graph alternative.
- `FITT_ROADMAP.md` Phase 9 draft — the source this spec promotes.
- Existing memory: `gateway/src/gateway/memory.py` (MemoryStore,
  decay), `gateway/src/gateway/lessons.py`, Phase 5 spec.
