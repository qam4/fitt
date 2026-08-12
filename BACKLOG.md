# FITT Backlog

Cross-cutting work that isn't owned by any phase spec - plus the one
thing that can't be derived: what to pick up next.

**Not** a tracker or a schedule. Per guiding Principle 9 (*live with it
before extending it*), items get picked when an evening goes that way,
not worked top to bottom. If it stops being a quick scan, prune it.

## What lives where

- **Phase work** -> `fitt tasks` rolls up every
  `.kiro/specs/*/tasks.md` (collapsing shipped/shelved phases via each
  spec's `**Status:**` line). The specs are the source of truth; don't
  copy their tasks here.
- **Big arcs** (Phase 8 compaction, 9 memory v1, 10 voice, 11 home
  assistant) -> [`FITT_ROADMAP.md`](FITT_ROADMAP.md).
- **Raw findings** -> [`docs/observed-issues.md`](docs/observed-issues.md).
  Items *graduate* from there into here when they turn from "noticed"
  into "worth doing."
- **This file** -> only items with no phase-spec home, plus the
  Now/Next/Later ordering.

**Lifecycle:** observed-issues (noticed) -> BACKLOG (worth doing) ->
spec (building) -> done.

## Now / Next / Later

The curated ordering - the judgment call a tool can't make for you.

**Now**
- **Per-model-family handling: keep it configuration, not code (framing,
  2026-08-10).** Today's session shows some model behaviour genuinely
  IS family-specific, so decide the shape before it leaks into `if
  model == ...` branches. FITT already does per-model *configuration* —
  `num_ctx` (the most decisive fix of the session), per-alias iteration
  budgets (`AliasOrchestrationConfig`), per-(alias, step) system prompts
  (the `prompts:` block), alias->model binding. Principle 7 names the
  boundary: models are configuration, not architecture.
  Family-specific needs observed so far, all candidates for config or a
  thin metadata-keyed adapter rather than scattered branches:
  - thinking models (`reasoning_content`; qwen3 needed a planner nudge,
    gemma4's over-iteration may be related);
  - tool-call dialects (models that narrate JSON instead of emitting
    `tool_calls` — hermes3 does it intermittently); repair is
    family-flavoured;
  - prompt-size budget thresholds (granite degraded ~5k, gemma4 fine at
    6k).
  The capability ladder + reconciler is the intended mechanism: measure
  the model, record the profile, let config adapt which features it can
  drive. NOT: hand-rolled per-model templates (tried, falsified, see
  observed-issues).
- **Re-measure post-litellm-fix — DONE 2026-08-10.** qwen3:14b 5/6
  (unchanged), gemma4:12b-it-qat 4/6 -> **5/6** with no spirals, hermes3:8b
  4/6 -> 3/6 (within its known `web_search` flakiness). Table in
  observed-issues. Two follow-ups fell out:
  - **`memory_recall` — FIXED 2026-08-11.** Was three harness defects,
    no model defect (see observed-issues). qwen3:14b now scores 6/6
    objective / 6/6 judge on the seed set.
  - **One sample per model isn't enough to read a one-step move.** Use
    `--samples` for anything we intend to cite as a comparison.
- **Per-scenario setup hook — SHIPPED 2026-08-11.** `TaskScenario.setup`
  + `e2e_driver.plant_turn` plant state with the model out of the loop
  (real `append_turn`, indexer drained); a failing setup reports
  *inconclusive*, never a model verdict. Cron-cancel is the next natural
  user (cancelled vs never-created look identical in the end state).
- **Cross-session retrieval works; it's a model-selection question, not
  a FITT gap (corrected 2026-08-12).** First read of this was wrong.
  gemma4:12b-it-qat passes the cross-session scenario by calling
  `memory_search` and returning the planted fact — Phase 9 is proven end
  to end. qwen3:14b never attempts it. So the levers below are optional
  polish for weaker models, not required work:
  - **prompt guidance** — nothing tells the model to search memory when
    asked about something it doesn't know; and `memory_search` defaults
    to `scope="session"`, so it must also choose `scope="all"`;
  - **prefetch (Phase 9e)** — built, off by default, injects a
    `[Recalled context]` block and removes the tool choice entirely.
  If prefetch is switched on, teach the cross-session assertion about it
  first: it's a *fourth* recall channel, and the harness has already
  mis-scored three times by not knowing which channel carried a fact.
- **Multi-sample before citing a standing number.** hermes3:8b scored
  3/7 and 4/7 on consecutive identical runs (`memory_recall` flipped).
  Single-sample cells in the standing matrix are indicative, not
  reliable; use `--samples` for anything load-bearing.
- **Frontier explorer — the actual goal (spec drafted 2026-08-12).**
  A frontier agent that *interacts with* FITT to find issues, rather than
  a judge that grades fixed transcripts. See
  [`frontier-explorer`](.kiro/specs/frontier-explorer/requirements.md).
  The evidence for it is blunt: every real defect in the 2026-08-10..12
  sessions came from investigation (differential experiments, reading run
  homes, wire capture), while the judge agreed with a *wrong* objective
  verdict five times — once while the contradicting evidence sat in its
  own prompt. A passive judge inherits the harness's framing, so it can
  never question the harness. Scripted scenarios are the regression
  floor; discovery needs an agent with `talk_to_fitt` + `inspect` verbs,
  missions instead of rubrics, findings that must carry evidence, and a
  human-gated ratchet from confirmed finding to permanent check.
- **Scenario coverage — now a spec, in progress.** Was 7 of 34 tools;
  see [`e2e-full-coverage`](.kiro/specs/e2e-full-coverage/tasks.md).
  Status 2026-08-12: **all 34 tools have a deterministic contract check**
  (`fitt eval contracts`, no model or tunnel needed) and the judged set
  is **9 scenarios** — the original 7 plus `notify` (proactive push,
  passes on gemma4) and `cron_fires` (a job actually firing and
  delivering). Remaining in that spec: cron cancel/pause, repo-query
  scenarios, the approval flow (ask -> approve/reject/timeout), the
  skills loader, planned-mode orchestration, prefetch, and Telegram
  command handling.
- **`<|tool_response>` leaking into a user-visible reply.** gemma4's
  reply after a successful `cron_add` was the literal string
  `<|tool_response>` — a raw chat-template token. The tool worked and the
  objective check passed on the side effect, but the user would see
  garbage. Not yet separated: model emitting a stray token vs FITT
  failing to strip one. A reply consisting only of template tokens is
  cheap to detect and suppress, and worth doing regardless of cause.
- **Scenario cross-talk (small, will bite again).** Scenarios share one
  run home, so lessons / todos / crons / the index carry side effects
  between them — a `learn_add` in one scenario handed a later scenario
  its answer. Distinct fixtures per scenario fixed the instance; the
  class is open. Cheapest general fix is probably per-scenario lessons
  isolation, since that's the global channel.
- **A Windows CI leg — the missing observer (2026-08-10).** FITT deploys
  on Windows; both CI jobs are `ubuntu-latest`. That gap is why the
  cp1252 `UnicodeEncodeError` class recurred ~10 times: it can't fail on
  Linux, and it can't fail in an interactive local shell either (Python
  only falls back to the ANSI codepage when stdout *isn't* a terminal).
  A `windows-latest` matrix leg running the same lint/typecheck/tests
  would catch this and any other path/encoding/shell assumption. Costs
  CI minutes and will likely surface a handful of pre-existing
  Windows-only test failures the first time, so it's a deliberate
  sitting rather than a drive-by. Cheaper interim: the entry-point
  tests added alongside `stdio_encoding.py`, which fake a cp1252 stream
  on any OS.
- **Template pre-flight check — idea survives, its motivating example
  does NOT.** gemma4:12b-it-qat ships a stub template (`{{ .Prompt }}`)
  and that looked like the cause of its spiral; **falsified twice** (a
  corrected template changed nothing, and a probe proved ollama ignores
  the stored template for `/api/chat` entirely). Real cause was the
  litellm bug above. A cheap `/api/show` sanity check on declared-vs-
  actual capabilities may still be worth having in the capability
  ladder's tool-check rung, but it is no longer "high value" and it has
  no known failure it would have caught. Deprioritised.
- **Executor-loop brake — SHIPPED 2026-08-10.**
  `Config.loop_brake_enabled` (default on) suppresses re-execution of a
  tool call whose (name, args) already succeeded this turn, injects a
  corrective tool result, and stops with `tool_loop_repeated` after 3.
  A/B'd in `tests/e2e/test_loop_brake.py` (off: 10 duplicate todos + 504;
  on: 1 todo + 200). Worth keeping even now that the underlying transport
  bug is fixed — a brake is standard, and it caps waste for any looping
  model. Known limitation: exact-signature matching misses
  near-duplicates (gemma4 slipped a second cron past it with slightly
  different args).
- num_ctx: per-model `ModelConfig.num_ctx` SHIPPED (router forwards to
  ollama). Remaining: a **boot-time warning** when a model's num_ctx is
  below FITT's prompt budget (Principle 11 — turn the silent
  `output_tokens=1` into a loud startup error). Small follow-up.
- Eval VRAM hygiene SHIPPED — `warm_status.py` + `fitt eval e2e
  --exclusive` evict co-resident models + warm the DUT + report VRAM/ctx,
  so contention can't silently pollute a measurement.
- Judged e2e harness — **SHIPPED 2026-08-07**
  ([`judged-e2e-harness`](.kiro/specs/judged-e2e-harness/tasks.md)).
  `fitt eval e2e` drives seed scenarios (reminder / news / memory-recall
  / todo) through the real pipeline against a live DUT, checks the
  *objective* side effect (primary), and optionally scores reply quality
  with a frontier judge (off by default). First live run caught + fixed
  four harness bugs and showed hermes3:8b hallucinating success on 3/4
  (see observed-issues). The frontier judge is wired + validated live via
  `--judge --judge-command "kiro-cli chat --no-interactive"` (it agreed
  with the objective layer). Seed set now: chitchat, reminder,
  news_summary, memory_recall, todo, todo_lifecycle. Follow-ups:
  configure web_search + retrieval so the news / memory-recall scenarios
  can pass; add a learn-a-lesson scenario (needs a small lessons slice in
  `snapshot_app`); a trustworthy cron-cancel needs a per-scenario setup
  hook (cancelled vs never-created look identical in the end state).
- Phase 9 (Memory v1) — **SHIPPED 2026-07-02 (9a–9g)**. Home-grown
  SQLite FTS5 + embeddings behind a `RetrievalProvider` ABC (Honcho
  rejected by the 9a spike); async indexer, `memory_search` tool,
  opt-in prefetch, `fitt memory reindex`/`status`. Recall quality
  validated with real `nomic-embed-text` on the local CPU Ollama (a
  3-week-old turn recalled #1). Off by default (opt-in via
  `memory.embedding_alias`; config snippet in observed-issues). Live
  with it before extending (Principle 9).

**Next**
- (open) Live with Phase 9 in real use, then pick a bigger arc (Phase 10
  voice / 11 home assistant — see roadmap). No small item queued.
- (Phase 5 closed 2026-07-02: validation reconciled to automated
  tests; see roadmap.)

**Later**
- **Self-improving loop: e2e harness -> coding agent -> FITT (IDEA ONLY,
  2026-08-10).** Product-owner idea: let the judged e2e harness feed a
  coding agent so FITT improves over time without the operator driving
  each fix. Not scheduled; captured so the reasoning isn't re-derived.
  Today's session is the evidence base for what each layer can actually
  do:
  1. **Regression gate (autonomous today).** The *objective* scenario
     layer is deterministic and needs no LLM. It already caught real
     defects (10 duplicate todo writes; num_ctx starvation). Running just
     this on a cron with `--samples` is genuine unattended value and does
     not depend on the judge getting smarter. **Start here if we ever
     start.**
  2. **Triage (judge).** Good at *behaviour* ("finish_reason=tool_calls
     every iteration, never terminates"), and Tier 2/3 make that specific.
     Output should be a hypothesis + evidence bundle, not a fix.
  3. **Investigation (agent, the missing piece).** The judge **missed the
     real root cause while holding the smoking gun verbatim** and blamed
     the model instead. Finding it took five *constructed* experiments:
     minimal working repro outside FITT -> step it toward FITT one
     variable at a time -> the step that breaks localises it -> factorial
     confirm. That is differential debugging, an *agent* capability
     (write + run new probes), NOT something a judge reading trajectories
     can do. Any real loop needs this layer.
  4. **Gated change.** Agent must ship fix + a NEW test that fails before
     and passes after. The loop-brake A/B (`tests/e2e/test_loop_brake.py`)
     is the template.

  Hard-won preconditions for unattended operation, all learned the hard
  way today:
  - **Fail closed on verdicts.** A truncated judge reply inverted a FAIL
    into a PASS; a loop acting on that "fixes" phantoms. (Fixed, but the
    class of bug is the point.)
  - **Kill confounds or chase ghosts.** Three stacked at once (num_ctx,
    VRAM contention, a template red herring). Needs the hygiene now built:
    isolated `FITT_HOME`, `--exclusive` VRAM, and a **pinned** judge model
    (on `auto` the grader itself drifts between runs, invalidating A/Bs).
  - **Never decide on one run.** news_summary passed one run and failed
    the next; use `--samples` + pass-rate thresholds.
  - Beware teaching-to-the-test: keep generic reasoning prompts separate
    from known-failure-mode checklists, or we mistake recall for
    discovery.

- Render the profile baseline-diff in the Capability card (folds into
  the 12.5b surface).
- Liveness bullet: fresh-shallow vs stale-deep + no auto-refresh
  (observed 2026-07-01; belongs to phase7.6-probe-clarity).
- Further capability-profile dimensions (VRAM, token-cost, JSON-
  validity, refusal rate, variance, context-degradation) - pulled in by
  12.5c reconciler demand.

---

## Capability, eval & observability

- **Capability surface + feature<->capability reconciler** - SHIPPED
  2026-07-01: [`phase12.5-capability-surface`](.kiro/specs/phase12.5-capability-surface/tasks.md).
  FITT's "detect optimal settings" layer (Principle 12): run the profile
  from the dashboard (12.5a, the no-CLI unblock), consolidate
  probe/eval/profile into one cost-tiered Capability surface (12.5b),
  and add the reconciler - per-feature `satisfied/unsatisfied/unknown`
  readiness + a boot warning, surfaces never auto-drives (12.5c). All
  three sub-phases shipped; V1-V5 hub validation closed by operator.
  The vocabulary this thread kept re-deriving now lives in
  project-overview steering ("Model capability: the measurement ladder").
  _(was: this session's "how does benchmarking inform config" thread.)_

- **Render the profile baseline-diff in the Capability card** - the
  card now shows declared facts + measured grades + resources from
  `<alias>-profile.json` (shipped 2026-06-25); the remaining piece is
  rendering the last baseline diff / regressions alongside it.
  _(detail: [phase12 deferred](.kiro/specs/phase12-planning-execution/tasks.md))_
- **Profile as single source of truth for capability** - the eval-report
  JSON sidecar + dashboard structured read shipped 2026-06-26 (the
  markdown round-trip is gone). What remains is the broader reframe:
  probe = liveness pip, profile = aggregation, fold the scenario result
  in as a dimension. Lower priority - the painful part is done.
  _(detail: [phase12 deferred](.kiro/specs/phase12-planning-execution/tasks.md))_
- **Capability-profile dimensions beyond v1** - VRAM/cold-load,
  token-cost-per-outcome, JSON-validity, refusal rate, run-to-run
  variance, context-degradation curve. Data model already supports each
  as an append.
  _(detail: [phase12 deferred](.kiro/specs/phase12-planning-execution/tasks.md))_
- **Better news search backend** - investigated 2026-06-26: ddgs
  `.text()` returns homepages for generic "today's news" queries but
  rich results for specific ones, and ddgs `.news()` is broken (Yahoo
  DNS refused). Not a small provider fix - a working news backend is a
  new provider file + config; query shaping is model-side. Low priority
  unless the news use case matters.
  _(detail: [observed-issues 2026-06-26](docs/observed-issues.md))_

- **Long-running dashboard actions are synchronous (async job + poll)** -
  validated 2026-06-30: "Measure capability" (and "run eval") hold the
  HTTP request for the whole run - minutes, because the planner pass is
  slow. Fine for now (operator-initiated, they expect a wait), but it
  ties up the connection and offers no progress. The clean fix fits what
  the dashboard already has: kick off a background run that returns
  immediately + a status fragment that polls every ~2s via HTMX
  (`hx-get` + `hx-trigger`) and swaps in the result when done - the
  job-id + status-poll shape the eval endpoint comment already flagged,
  and exactly what kiro-monitor models. The profiler's phases
  (tool-calling -> coding -> plan-election) give natural progress text;
  no progress-bar framework needed. Applies to `/v1/profile` and
  `/v1/eval` alike. Graduate when the wait bites.
  _(source: phase 12.5a live use; see phase12.5 spec deferred)_

- **Monitor prompt size against the model's input budget (and act when
  over)** - the granite lesson, generalised. Small/quantized local
  models degrade at a *total* prompt-token threshold well below the
  context window (granite: clean at 141 tok, narrated at ~5K, on a 256k
  window). FITT's per-turn prompt plateaus at `fixed overhead
  (capability+skills+identity+lessons) + history cap`, so the question
  is whether that plateau sits under the bound model's degradation
  threshold. **Have:** per-turn prompt-token count is already visible
  (`/lastturn`, `history_truncated_bytes`). **Missing:** (1) the
  threshold itself - the "context-degradation curve" profile dimension
  (listed under profile dimensions above) that measures where THIS model
  falls off; (2) a live guardrail that compares prompt tokens against it
  and warns / acts. **Act =** the levers: trim fixed overhead
  (`compact_capability_block`-style), lower `max_history_chars`,
  retrieval (Phase 9, precision-preserving - the good one), or
  compaction (Phase 8, lossy). This is the measure->adapt loop
  (Principle 12) applied to prompt size. Start with the measurement,
  add a boot/dashboard warning; auto-acting is later.
  _(detail + full framing: [observed-issues](docs/observed-issues.md)
  "Prompt-size budget")_

- **Judged end-to-end harness — SPEC'd 2026-07-02
  (`.kiro/specs/judged-e2e-harness/`).** The missing rung: every
  real-model check today grades *structurally* (did the right tool
  fire / did the loop finish) — nothing verifies a feature's *outcome*
  end to end ("give me a news summary" is graded did-it-fetch, not
  is-it-good). This adds it: drive a natural-language request through
  the real pipeline against a real DUT (`fitt-ec2-qwen3` over the
  tunnel), **assert the actual side effect** (cron created for the
  right time, todo added — the primary, judge-free layer), and
  **optionally have a frontier judge (kiro-cli) score the fuzzy reply
  quality** (off by default). Modeled on chess-coach
  `game-coaching-eval`; extends `scenario_eval`. Sequenced to also
  drive the new `todo_*` feature test-first (Phase E). Dev-driver, not
  a CI gate.
  _(source: this session; ties to the reminder + todo feature asks)_

## Tool ergonomics & coverage

- **Eval harness should exercise the REAL registered tools** - today it
  tests synthetic re-declared schemas, so schema-ergonomics bugs in the
  shipped registry (the `cron_add` failure) are invisible by
  construction. Prerequisite for the two below.
  **Framing (see project-overview "measurement ladder"):** two distinct
  subjects, don't conflate. (a) *Model* - can it tool-call? The eval
  measures this and a representative handful of cases is enough; feeding
  it the *real* tool forms (not lookalikes) is the small targeted fix, so
  the shipped `cron_add`/`edit_file` shapes finally face a model. (b)
  *Tools* - are the forms consistent/callable? That's a separate, cheap,
  *offline* check that reads whatever's registered (incl. MCP + skills
  that no hand-written per-tool case could ever cover). Don't try to
  live-eval every tool - the ladder tests the model with representatives,
  not the inventory.
  **Lane (b) SHIPPED 2026-07-01:** `tool_consistency.py` -
  `check_tool_consistency(tools)` flags text-payload-family arg drift
  (an explicit family - send_message / learn_add / cron_* - keyed on a
  canonical `text`, so git_commit's `message` for a *commit* message is
  NOT a false positive) and empty descriptions; logged at boot
  (`tools.inconsistent_schema`) and surfaced on the Settings "Boot-time
  warnings" card (which now aggregates all boot checks incl. MCP/skills).
  The required-field budget + name-collision rules stay with
  `test_tool_schema_lint.py` (the CI gate, which handles reviewed
  exceptions a flat pass can't). **Lane (a) SHIPPED 2026-07-02**
  (`phase12.6-eval-real-registry`): the default + realistic suites name
  real tools and source their live `to_openai_schema()` via
  `resolve_case_tools`; the coding suite stays synthetic on purpose
  (models an external coding-agent, not FITT's registry). Re-baselined
  on gemma4 at parity with the old lookalikes.
  _(source: [observed-issues](docs/observed-issues.md))_
- **Normalise "the words" tool-arg naming** - SHIPPED 2026-07-01:
  `cron_add` / `cron_update` payload arg renamed `message` -> `text` to
  match `send_message` / `learn_add`; the internal `CronJob.message`
  field is unchanged (no on-disk migration). The Lane (b) family lint
  guards against future drift.
- **Flatten `edit_file`'s fumble surface** - SHIPPED 2026-07-01: the
  zero-match error now quotes the closest on-disk text (line-windowed
  difflib; usually reveals a whitespace/indentation mismatch) and the
  >1-match error names the line numbers where old_str starts. The 4
  required fields are kept (legitimate); no field-count change.
- **Planner pass shouldn't execute tools it didn't offer** - gemma4 calls
  an executor tool from the planner pass (side effect of the
  executor-tools hint).
  _(source: [observed-issues](docs/observed-issues.md))_

## Opportunistic upgrades (OpenClaw-inspired)

- **Setup recipes the agent can drive** - "help me set up X" docs written
  *for* the agent (numbered steps, exact commands, fallbacks). ~half a
  day per recipe.
  _(source: project-overview steering)_
