# Judged End-to-End Harness: Requirements

## Introduction

Every real-model check FITT has today grades **structurally**, never
on outcome quality:

- **probe** (`alias_probe`) — did the model emit *a* tool_call.
- **eval** (`alias_eval`) — did it emit the *expected tool shape*
  (right tool name), multi-sampled.
- **scenario** (`scenario_eval`) — did the multi-step loop *complete
  the expected shape*. `classify_news_outcome` marks "give me a news
  summary" **completed** when the model fetched live info and returned
  a substantive reply — it never checks whether the summary is
  *correct or good*.

So nothing answers "**does this feature actually work end to end for
the user?**" — did FITT set the reminder for the right time, produce a
useful summary, add the todo. This harness adds that rung: drive a
natural-language request through the **real pipeline against a real
model**, verify the **actual outcome** (the side effect / ground
truth), and optionally have a **frontier judge (kiro-cli)** score the
fuzzy reply quality.

Modeled on the chess-coach `game-coaching-eval` harness: a pure,
I/O-free core (scenario → trajectory → objective checks → optional
judge) under a thin driver that wires the real app + a real DUT model
+ the judge. The objective layer stands alone (judge off by default),
so the tool is useful without a judge; the judge is the fuzzy add-on.

## Where this sits in the measurement ladder

`ping → probe → eval (structural) → scenario (structural) → **judged
e2e (outcome + quality)**`. It's the top rung: the only one that asks
"did the feature produce the right *result*," not just "did the right
tool fire." Complements, does not replace, the structural rungs.

## Glossary

- **DUT** — the model *under test*, bound to a FITT alias and driven
  through the real pipeline (default: `fitt-ec2-qwen3` over the EC2
  tunnel). The thing whose end-to-end behavior we're checking.
- **Judge** — a *frontier* model, stronger than the DUT, that scores
  the fuzzy quality of the DUT's reply against a rubric. Default
  transport: kiro-cli (headless). Never the runtime; eval-only.
- **Task scenario** — a natural-language request (one or more turns) +
  the **objective outcome assertion** (what must be true afterward) +
  an optional **judge rubric** for reply quality.
- **Outcome assertion** — a judge-free, deterministic check of the
  real end state: e.g. "a one-shot cron exists with `at_ts` ≈ tomorrow
  09:00 in the user's tz." This is the "did FITT get it done" layer.
- **Trajectory** — the run reduced to a serializable record: the
  turns, the dispatched tool_sequence, the reply, and a **side-effect
  snapshot** (cron store / todos / memory as of run end) + any
  captured ground truth. Replayable and re-judgeable.
- **Verdict** — the judge's rubric result for a scenario's reply.

## Requirements

### R1. Drive a scenario end-to-end against a real model

As an operator, I want a natural-language request run through FITT's
real pipeline against a bound model, so the test reflects what a user
actually gets.

**Acceptance:**

- **1.1** The harness SHALL send a task scenario's turn(s) through the
  real chat pipeline (auth → memory → tool loop → dispatch) against a
  configured DUT alias, reusing `scenario_eval`'s real-model driver.
- **1.2** Multi-turn scenarios SHALL be supported (e.g. a request, then
  a follow-up), so features that span turns (a reminder set now, fired
  later) can be exercised.
- **1.3** A scenario SHALL be multi-sampleable (k runs) for a pass
  rate, since both DUT and judge are non-deterministic (the Phase 12
  conventions).

### R2. Capture a serializable, replayable trajectory

As a developer, I want the run captured so failures are debuggable and
re-judgeable without re-running the model.

**Acceptance:**

- **2.1** Each run SHALL produce a serializable trajectory: turns,
  `tool_sequence`, assistant reply, and a **side-effect snapshot**
  (relevant store state at run end), with `to_dict`/`from_dict`.
- **2.2** The DUT dispatches SHALL be recordable (reuse the
  `RecordingRouter` cassette) so a failing run can be replayed
  deterministically to see exactly what the model emitted.
- **2.3** A saved trajectory SHALL be re-judgeable without replaying
  the model (judging reads the trajectory, not the live loop).

### R3. Objective outcome assertions (judge-free) — the primary layer

As an operator, I want each scenario to verify FITT *actually did the
thing*, deterministically, so a passing text reply can't mask a
missing side effect.

**Acceptance:**

- **3.1** Each scenario SHALL define an **outcome assertion** that
  reads the real end state (cron store, `todos.md`, memory, event log,
  …) and returns pass/fail with a reason — no LLM involved.
- **3.2** The reminder scenario's assertion SHALL check a one-shot cron
  was created with `at_ts` within tolerance of the requested time in
  the correct timezone.
- **3.3** A scenario with no meaningful side effect (e.g. a pure
  Q&A/summary) MAY define a trivial/absent outcome assertion and rely
  on the judge — but the harness SHALL still record the tool_sequence
  and reply.
- **3.4** The harness SHALL be fully useful with the objective layer
  alone (judge disabled): it runs, asserts outcomes, and reports.

### R4. Optional frontier judge for reply quality

As an operator, I want a frontier model to score the fuzzy quality of
the reply (helpful? correct? no hallucination?) against a rubric, so I
catch quality regressions the structural checks can't.

**Acceptance:**

- **4.1** Per scenario, when judging is enabled, the harness SHALL send
  `(intent + rubric + reply + tool_sequence + outcome result)` to the
  judge and record a verdict (pass/fail + score + reasoning).
- **4.2** The judge SHALL be a configurable provider defaulting to
  kiro-cli (headless), run at temperature 0; it MUST out-class the DUT
  (never the DUT judging itself).
- **4.3** Judging SHALL be **off by default** and **failure-isolated**:
  a judge/parse error on one scenario yields an un-judged result, not
  an aborted run (parallels the eval harness's per-item handling).
- **4.4** The news-summary scenario SHALL be judged on summary
  *quality* (grounded, substantive, on-topic) — the gap this whole
  harness is motivated by.

### R5. Reproducibility

**Acceptance:**

- **5.1** A run SHALL record its config (DUT alias, judge, samples,
  seed where applicable) in the trajectory metadata.
- **5.2** Non-determinism that can't be removed (sampling DUT, thinking
  judge) SHALL be documented, not hidden — the harness is a
  dev-driver + lenient/multi-sample signal, NOT a strict per-commit
  gate.

### R6. Run mechanics

**Acceptance:**

- **6.1** The harness SHALL ship as a `fitt`-CLI/`scripts` driver over
  a **pure core** (scenario → trajectory → assertions → optional judge)
  that is unit-testable with fakes (fake dispatch, fake judge) — no
  live model, no kiro-cli, in the core tests.
- **6.2** DUT alias, judge on/off, judge command, sample count, and
  output dir SHALL be configurable.
- **6.3** The driver SHALL be safe to launch under kiro-monitor
  (real-model + judge runs take minutes).

### R7. Seed scenarios

**Acceptance:**

- **7.1** Ship at least: **reminder** (cron one-shot; objective
  assertion on the cron store), **news summary** (existing scenario,
  now quality-judged per 4.4), **todo curation** (drives the new
  `todo_*` feature — objective assertion on `todos.md`), and **memory
  recall** (Phase 9 — a multi-turn scenario: state a fact, then later
  ask about it; objective assertion = `memory_search` fired AND the
  retrieved excerpt matches the earlier turn; judge = was the recalled
  answer grounded). The memory recall case is what verifies Phase 9
  end-to-end — the one link the shipped tests don't cover is whether a
  real model *decides* to call `memory_search` (the provider core +
  the indexer→index→tool chain are already proven with real
  embeddings; only the model's tool-call decision is untested).
- **7.2** Adding a scenario SHALL require only a scenario definition +
  its outcome assertion (+ optional rubric) — no bespoke classifier per
  scenario (the scaling win over `classify_news_outcome`).

### R8. Quality gates

**Acceptance:**

- **8.1** `uv run pytest`, `mypy src` (strict), `ruff check`,
  `ruff format --check` green in both packages.
- **8.2** The pure core (scenario loop, trajectory build/serialize,
  assertion runner, aggregate) SHALL be unit-tested with fakes — no
  live model/judge in CI.
- **8.3** A CI-safe smoke SHALL exercise the driver end-to-end with a
  stubbed model + fake judge (proving wiring), separate from the live
  runs.

## Open decisions (resolve before design/tasks)

- **OD1. Judge transport.** kiro-cli headless contract — plain
  `stdin prompt → stdout verdict`? (chess-coach's `CliProvider` /
  `--judge-command` implies yes.) Confirm so the judge provider models
  the same contract. Fallback: a strong cloud alias via FITT's own
  OpenRouter routing.
- **OD2. DUT default.** `fitt-ec2-qwen3` over the tunnel (assumed).
  Confirm, and confirm tunnel availability is a run prerequisite (the
  harness skips/warns cleanly when the tunnel is down, like the eval
  CLI).
- **OD3. Scenario priority.** Reminder + news + todo first (R7.1);
  confirm order and whether todo drives the feature build (spec the
  todo tools alongside, or after).

## Non-goals

- **Not a per-commit CI gate.** Non-deterministic + needs a live model
  + judge. It's a dev-driver + lenient multi-sample signal.
- **Not a replacement** for the structural probe/eval/scenario rungs —
  it sits above them.
- **The judge is eval-only** — a frontier model here never becomes the
  runtime router target.
- **No new judging of already-structural things** — don't LLM-judge
  what an outcome assertion covers deterministically.

## References

- chess-coach `game-coaching-eval` spec + `eval/game_coaching.py` — the
  proven template (pure core + driver, objective fidelity layer +
  optional frontier judge, judge-off-by-default, replayable trajectory,
  kiro-monitor).
- `gateway/src/gateway/scenario_eval.py` — the real-model multi-step
  driver this extends; `alias_eval.py` conventions (multi-sample,
  transient exclusion); `record_replay.py` (cassettes).
