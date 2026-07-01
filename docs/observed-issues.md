# FITT — Observed Issues

A running log of friction, bugs, and small design problems
noticed in live use. Reverse-chronological (newest first).

Not a triage system. Not a bug tracker. A record of what we've
been living with so a future scan ("what small things have we
noticed?") finds them in one place. Some entries will graduate
into spec-level work; some will stay notes; some will quietly
become irrelevant and we'll delete them.

Related docs:

- [`docs/hallucinations-and-poisoning.md`](./hallucinations-and-poisoning.md)
  — deeper framing for the model-level and context-level
  reliability issues below. Several entries here cross-reference
  its four-problem breakdown (A: hallucination, B: poisoning,
  C: self-deception, D: invisibility).
- [`docs/choosing-a-model.md`](./choosing-a-model.md) — how to
  pick which model to bind to a FITT alias. Some entries here
  are downstream of an unfortunate model choice.
- [`FITT_ROADMAP.md`](../FITT_ROADMAP.md) — direction and phase
  plan. When an entry here starts to hurt enough to shape a
  phase, promote it into a spec there.

## Entry format

Each entry has a short slug heading, the date first observed,
and roughly: what we saw, what it costs, what the fix looks
like (if any), and how urgent it feels. Keep it short — if
you're writing more than a screen, it probably wants its own
doc.

---

## Liveness bullet conflates fresh-shallow reachability with stale-deep probe; nothing auto-refreshes

**First observed:** 2026-07-01 (walking the ping/probe/eval/profile
vocabulary; see project-overview "measurement ladder").
**Tag:** Phase 7.6 / probe clarity / dashboard freshness.

Two findings from reading the reachability + probe wiring:

1. **The colored alias bullet shows the probe (a tool call), not
   reachability.** `_probe_pip(probe.status)` drives it. So "green"
   means "last tool-call probe was ok", not "host is up right now".
   The two answer different questions: reachability is cheap and
   shallow (a granite-shape model is reachable but narrates -> would
   show green wrongly); the probe is deep but expensive.
2. **Nothing refreshes either signal on a schedule.** The probe runs
   at boot (`_run_boot_probe`) and on manual re-probe only - no
   periodic task (cron/pruners/context-populate are the only loops).
   Reachability (`reachability.check_reachable`) is recomputed fresh
   only when `/ready` is hit, and isn't stored on the alias state the
   dashboard reads. So on a long-lived gateway the bullet reflects a
   boot-time probe that can be days old - which is exactly why 12.5b
   added a "stale" flag to the liveness line.

**Cost:** a stale-green bullet can imply an alias is healthy when it
isn't (or vice versa) - low-frequency but misleading during an
incident.

**Fix shape:** decide what the at-a-glance bullet is *for*. If it's a
live "is this up" dot, back it with the cheap signal (reachability)
and add a small periodic ping so it's actually fresh; show the deeper
probe verdict as a separate, timestamped line ("tool-call: ok, 2d
ago"). If it stays probe-backed, surface staleness everywhere (the
alias page does now; the aliases-table bullet still doesn't). The
underlying gap is "no background refresh of either signal" - fix that
before bikeshedding the color. Belongs to the `phase7.6-probe-clarity`
lineage (owns pip semantics + the amber/green/red rules), not the
eval-harness thread this surfaced from.

**Urgency:** low. A note, not a fire.

## Capable model (qwen3:14b) synthesizes from real search content - relay was model weakness, not prompt

**First observed:** 2026-06-26 (synthesis-vs-relay retest; BACKLOG follow-on
to the task-26 verdict).
**Tag:** Phase 12 / synthesis vs relay / model-fit / web_search.

Retested the task-26 relay-vs-synthesize question on a *capable* model with
*real* content. Added a `topic_brief` scenario - a specific query ("recent
discoveries from the James Webb Space Telescope") concrete enough that ddgs
returns content-rich results (per the ddgs finding above), removing the
thin-search confound that muddied the hermes3 runs. fitt-ec2-qwen3
(qwen3:14b), flat, 3 samples.

Result: 2/3 `completed`; the deciding signal is reading the replies -
**both content-bearing samples SYNTHESIZED.** Clean 3-5 bullet summaries in
qwen3's own words with inline source attribution (Carnegie Science /
GLASS-JWST, NASA NIRSpec / comet 3I/ATLAS, ESA Butterfly Nebula,
ScienceDaily early galaxies, ...) - NOT a relay of raw title/URL/snippet
lines. The opposite of hermes3:8b's relay. Sample 2 also *retried* a failed
search (`web_search:error -> web_search:ok`) - agentic recovery hermes3
never showed.

**Verdict: the relay-vs-synthesize failure is MODEL CAPABILITY, not the
prompt.** A capable model synthesizes given real content under the *existing*
capability-block guidance; hermes3:8b relays because it's too weak, not
because the prompt is wrong. This confirms the task-26 caveat and refines
the Phase 12 conclusion: the lever for output quality is a capable executor,
not harness/prompt tuning. (It also retroactively justifies reverting the
"synthesize, don't relay" capability-block change - it wasn't needed; the
prompt was already adequate.)

Caveats / observed oddities:

- **ddgs flakiness recurred**: `web_search` returned `provider_failed` on the
  first call in 2 of 3 samples (rate-limiting). qwen3 recovered by retrying
  (sample 2); sample 1 never got results.
- **Off-topic hallucination on total search failure (sample 1):** with the
  search fully failed, qwen3 acknowledged "the web search failed" but then
  pivoted to an unrelated **cake recipe** instead of staying on JWST or
  honestly stopping. A bizarre n=1 oddity - when it has no data it should say
  so, not generate unrelated content. Worth watching.
- n=3, one topic, one model. A clear signal (2/2 content-bearing samples
  synthesized), not a law.

Tooling: added the reusable `topic_brief` scenario
(`fitt scenario run <alias> --scenario topic_brief`).

---

## ddgs returns homepages for generic news queries: query shaping, not the backend

**First observed:** 2026-06-23 (Phase 12 synthesis experiment).
**Investigated:** 2026-06-26.
**Tag:** web_search / ddgs / query shaping.

The daily_news_summary experiment saw `web_search` return news-site
homepages (Google News / NBC / CBS / NYT) instead of headlines.
Investigated by probing ddgs directly (the search runs on the gateway
host - no model involved), four queries:

1. `text("today top news headlines")` -> HOMEPAGES (Google News, AP,
   WaPo, NBC, CNN) with boilerplate snippets. Reproduces the problem.
   Caveat: a couple snippets *did* carry a real headline (AP/Google's
   "Venezuela earthquakes kill at least 235"), so a model that reads
   snippets could extract *some* news - the weak model relaying the
   raw list is a compounding factor.
2. `news("top news today")` -> ERROR. ddgs's news endpoint routes via
   Yahoo (`news.search.yahoo.com`) and returned a DNS "Query Refused".
   The dedicated news endpoint - the right tool for headlines - is
   broken in the current ddgs.
3. `text("world news", timelimit="d")` -> a single Wikipedia "World
   news" concept page. timelimit doesn't help.
4. `text("Iran nuclear agreement news")` (SPECIFIC) -> relevant,
   content-rich results (Wikipedia 2025-26 Iran-US negotiations, CFR,
   a Jun-2026 video, ICAN). A specific query returns useful results.

**Diagnosis: the homepage problem is QUERY SHAPING, not a backend
limitation.** ddgs `.text()` returns rich, on-topic results for
specific queries and homepages for generic "today's news" ones. The
`.news()` endpoint that would fix the generic-headlines case is broken
(Yahoo DNS refused); `timelimit` is no help.

**Implications:**

- Not a small provider code fix. Levers: (a) get the model to issue a
  *specific* query - hard for the inherently-broad "summarize today's
  news"; or (b) a working news backend (DDG news is broken here; a real
  news API or a different provider would be needed - the provider plugin
  layer makes that a config + one-file add).
- For the synthesis-vs-relay retest (the other open item): use a
  SPECIFIC-topic query so there's real content to synthesize. That
  removes the thin-search confound and makes it a clean test of
  synthesis vs relay, decoupled from this ddgs limitation.

**Tooling:** probed with a throwaway `ddgs_probe.py` against the venv's
ddgs (deleted after).

---

## Synthesis-over-relay capability-block tuning didn't help hermes3:8b (reverted)

**First observed:** 2026-06-23 (test of the task-26 "under-prompted" verdict).
**Tag:** Phase 12 follow-on / capability-prompt tuning / web_search quality.

Task 26 concluded the daily_news_summary failure was "under-prompted"
and pointed at strengthening the synthesis instruction. Tested it.
Note: orchestration is off by default, so the live flat-loop lever is
the capability block (`capabilities.py`, the [Using tools for current
facts] section), NOT the execute-step prompt resolver (which only
fires in planned/orchestrated mode). Strengthened that block: read the
snippets and SYNTHESIZE in your own words, do not paste/number/list
titles/URLs/'Snippet:' lines, and if the results don't contain the
answer say so + suggest a better query.

A/B on fitt-ec2-hermes (hermes3:8b), flat, 3 samples each, same day,
reading the actual replies (the length classifier scores all as
`completed`):

- Baseline (old block): 0/3 synthesized - one send_message-schema
  derail, one verbatim relay, one meta-relay.
- Treatment (new block): 0/3 synthesized, arguably worse - all 3
  abandoned the news task to narrate tool schemas (todowrite /
  project_shell / learn_remove).

**Verdict: the "under-prompted" hypothesis is NOT supported on
hermes3:8b.** A stronger synthesis prompt didn't help and plausibly
hurt - consistent with the documented "longer prompt degrades
hermes3:8b" effect (see the planner tool-blindness entry) and the
broader Phase 12 conclusion that the 8b is the bottleneck, not the
harness. **The capability-block change was reverted.**

Two confounds keep this from being a clean test:

1. **Search quality.** ddgs returned news-site HOMEPAGES (Google
   News / NBC / CBS / NYT) with boilerplate snippets, not actual
   headlines (task 26 days earlier got real headlines like "Trump
   says agreement with Iran is not final"). With no real content
   there is nothing to synthesize, so relay-vs-synthesize can't be
   judged. A prerequisite to fix before retesting.
2. **Model weakness / prompt load.** hermes3:8b's tool-schema-
   narration degeneracy dominates at n=3.

**A clean retest needs both:** a capable executor (qwen3:14b) that can
actually obey "synthesize, or honestly report thin results", AND a
search query/backend that returns real headlines. Until then the
prompt change stays out of the tree.

**Kept:** the harness change that made this readable - a
`--preview-chars` option on `fitt scenario run` (default 200) so the
operator can read full replies, since only the text (not the
length-based pass rate) reveals synthesis vs relay (the task-4
mandate, "read the actual replies").

**Caveat:** n=3, one model, one task, garbage search input,
EC2-over-SSM (flaky). A clear signal, not a law.

**Urgency:** low. Negative result logged so it isn't blindly re-run;
the real follow-on is execute-step prompt tuning measured on a model
that can follow it, with working search.

---

## Open WebUI model picker went empty: PersistentConfig pinned the stale gateway port

**First observed:** 2026-06-23 (operator: "can't select a model in
Open WebUI anymore").
**Tag:** deployment / Open WebUI / PersistentConfig / config-as-code.

The Open WebUI model dropdown went empty. The gateway was healthy
and `GET /v1/models` returned the alias list correctly end to end -
proved by curling it from *inside* the open-webui container
(`docker exec fitt-open-webui curl -s http://gateway:8421/v1/models`
-> full JSON). The OWUI logs told the real story:

    ERROR [open_webui.apps.openai.main] Connection error:
    Cannot connect to host gateway:8080 ... Connect call failed

Open WebUI was dialing **`gateway:8080`** - the gateway's *old*
port - while the gateway now listens on 8421. Root cause:
`OPENAI_API_BASE_URL` is a **PersistentConfig** variable in Open
WebUI. The compose env (`http://gateway:8421/v1`) only SEEDS the
OWUI database on first boot; after that OWUI reads the value from
its own DB and ignores the env. The gateway had moved off port 8080
(it collides with QNAP's QTS admin UI - the exact collision
`.env.example` warns about) to 8421, but OWUI's DB kept
`gateway:8080`. Env said 8421, DB said 8080, DB won.

Red herring along the way: `curl http://nas-qnap:8080/v1/models`
returns a 302 to `https://nas-qnap:443/...` - that's QTS's admin UI
answering on 8080, not the gateway, and unrelated. The gateway is on
8421 and OWUI reaches it over the compose network, never the host
port.

**Cost:** a long debug. Every symptom pointed at the gateway
(healthy, yet an "empty" external curl) when the gateway was fine -
the failure was hidden state in OWUI's DB that no config file
revealed.

**Immediate fix:** edit the connection URL in OWUI Admin -> Settings
-> Connections to `http://gateway:8421/v1`. Works, but it's
click-ops living in the DB - it silently breaks again on the next
port change, volume reset, or fresh hub.

**Durable fix (shipped):** set `ENABLE_PERSISTENT_CONFIG=false` on
the open-webui service so OWUI re-reads its env on every boot and
the compose file is authoritative again. The compose env already
points at `gateway:${FITT_PORT}/v1`, so the connection self-corrects
and can't drift. Ripple: `ENABLE_SIGNUP` is also PersistentConfig,
so its UI toggle stops persisting - moved it to a declarative
`WEBUI_ENABLE_SIGNUP` .env knob (default false, fail-secure) with a
two-phase bootstrap, and rewrote quickstart step 16 to match. Trade:
Admin-UI config changes no longer survive a restart (intended for a
config-as-code hub; accounts and chats are unaffected - they are not
PersistentConfig).

**Lesson:** any Open WebUI setting exposed as an env var is
PersistentConfig by default - the env is decorative after first boot
unless `ENABLE_PERSISTENT_CONFIG=false`. Treat OWUI as config-as-code
from the start, or moving any of those values later silently no-ops.

**Urgency:** resolved (durable fix shipped). Confirm the flag is
honored on the pinned image after the next
`docker compose up -d open-webui` (v0.3.35; PersistentConfig shipped
in 0.3.0, so it is).

---

## Phase 12 verdict on daily_news_summary: the failure is the prompt, not the harness or the model

**First observed:** 2026-06-16 (Phase 12 task 26, live-validation close-out).
**Tag:** Phase 12 hypothesis verdict / planning value / prompt design.

Task 26 closes the Phase 12 measurement sweep with a verdict on the
guiding hypothesis — *"a weak model is under-harnessed, not incapable;
elected planning makes it competent on a multi-step turn."* Five live
experiments on `daily_news_summary` (fetch today's news, then summarize)
across hermes3:8b and qwen3:14b on EC2 (mirroring the hub's lineup):

| Experiment | Config | Result |
|-----------|--------|--------|
| Task 4 | hermes3 flat | 5/5 fetched, 0/5 synthesized (relay) |
| Task 22 | hermes3 planned | no delta vs flat |
| Task 23 | hermes3 as planner | plan election 0% |
| Task 25 | qwen3 plans, hermes3 executes | election 0->100%, execution still relay |
| Task 26 | qwen3 flat | marginally better, still mostly relay |

**The hypothesis did NOT hold cleanly for this case.** Walking the
levers:

- **Better harness (planning) didn't help** (22). hermes3 doesn't even
  elect to plan (23); when a capable planner forces a correct,
  re-injected plan (25), hermes3 still relays — so the bottleneck
  isn't sequencing/election, which is what planning's leverage is.
- **A more capable executor helped only at the margin** (26). qwen3:14b
  pulled some real headlines ("Trump says agreement with Iran is not
  final") and recovered from a failed search, where hermes3 never did
  — but 2/3 of its runs still relayed source listings/snippets.

The one factor invariant across every model and every harness is the
**relay-vs-synthesize tendency**: `web_search` returns titles + URLs +
snippets, and the models default to reformatting that structure rather
than reading it and writing original prose. That points at the
**execute-step prompt** (and the tool-result shape), not a capability
cliff or a harness gap.

**Verdict: for this case the framing should be "under-PROMPTED", not
"under-harnessed".** The lever is the execute-step prompt (Story 2.4
per-step tuning) — explicitly demand "read these results and write a
summary in your own words; do not list sources" — not planning and not
a model swap. Capability does matter at the margin (qwen3 > hermes3),
so the task-24 capability profile is still worth building; but it is
not the lever for *this* failure.

**Scope / honesty caveat (the n=1 discipline, applied):** this is ONE
multi-step task, n=3-5 per config, EC2-over-SSM (which dropped tunnels
mid-sweep — the runner now records those as transient and excludes
them). It is a clear, consistent signal, NOT a phase-wide law. A
sequencing-heavy task (where the failure IS step ordering, not prose
quality) would likely show planning earning its keep — that direction
is untested here and is exactly what a broader task-24 case set should
cover. The classifier limitation compounds the caveat: `completed` is
length-based and scores relay as pass, so this verdict rests on
reading the replies, not the pass rate.

**Actionable follow-ons (not this phase):**
1. Tune the execute-step prompt to demand synthesis over relay; re-run
   the sweep — the cheapest test of the "under-prompted" verdict.
2. task 24: per-dimension capability profile, including a sequencing-
   heavy case so planning's value is measured where it should appear.

---

## Planner_alias split (qwen3 plans, hermes3 executes): fixes election, not execution

**First observed:** 2026-06-16 (Phase 12 task 25, "concentrate intelligence in planning").
**Tag:** Phase 12 planner_alias / planning value / model-fit.

Task 25: the "concentrate intelligence in planning" experiment —
planner on qwen3:14b (capable, thinking), executor on hermes3:8b —
on `daily_news_summary`, 3 samples, via `fitt scenario run
fitt-ec2-hermes --mode planned --planner-alias fitt-ec2-qwen3`.

| Metric | hermes plans (task 23) | qwen3 plans (task 25) |
|--------|------------------------|-----------------------|
| plan election | 0% | **100%** |
| actually synthesized | 0/5 | 0/3 |
| pass rate | n/a (no plan) | 2/3 |

**The capable planner fixes the planning step, not execution.**
qwen3 produced a plan every time (election 0% -> 100%), and the
orchestrator re-injected it into hermes3's executor context. But
hermes3 still relayed raw search results instead of synthesizing
(2/3), and one run degenerated entirely (`no_search`): hermes3
emitted no tool calls and dumped pydantic JSON schemas of tools it
"will make" into the user-facing reply — 7 iterations, 21K tokens,
no actual search.

So "concentrate intelligence in planning" doesn't rescue a weak
executor on this task. The plan was present and correct; hermes3
just isn't capable enough to follow it through to a synthesized
answer. **The lever for this failure (execution output quality) is
a more capable executor, not a better plan** — planning's leverage
is sequencing/election, which wasn't the bottleneck here.

This refines the Phase 12 hypothesis ("elected planning makes a weak
model competent on multi-step turns"): planning helps when the
failure is *sequencing* (does it plan, does it call tools in order);
it does not help when the failure is *execution output quality*
(does the model write a good answer from tool results). The
daily_news_summary failure is the latter.

Caveat (n=1 discipline): 3 samples, one task, one model pair. A
clear, consistent signal (100% election, 0% synthesis), not a law.
The systematic per-dimension read is task 24.

---

## Flat-vs-planned comparison on daily_news_summary: no delta on hermes3:8b

**First observed:** 2026-06-16 (Phase 12 task 22, flat-vs-planned comparison).
**Tag:** Phase 12 hypothesis test / planning value / model-fit.

Task 22: same `daily_news_summary` scenario, same alias (hermes3:8b on
EC2), 5 samples flat vs 5 samples planned, via `fitt scenario run
--mode flat` / `--mode planned`.

Both modes score 5/5 `completed` (searched + substantial reply). But
reading the replies: **both relay raw search results instead of
synthesizing.** Planning did not fix the output quality gap.

| Metric | Flat | Planned |
|--------|------|---------|
| web_search called | 5/5 | 5/5 |
| send_message called | 1/5 | 1/5 |
| Actually summarized | 0/5 | 0/5 |
| Avg iterations | 2 | ~4.6 |
| Avg in_tokens | ~8190 | ~9428 |

Planning added overhead (~2.6 extra iterations, ~1.2K more input
tokens per sample) but the failure mode — "relay links/snippets
verbatim instead of writing a bullet summary" — persists unchanged.
One planned sample [5] went off the rails and dumped the `todowrite`
JSON schema into the user-facing reply.

**Why planning didn't help:**

1. The flat-loop failure here is NOT structural (missed tool
   calls / wrong sequencing). hermes3 already fetches reliably
   without a plan. Planning's leverage is multi-step sequencing;
   when the model sequences fine but writes badly, planning adds
   cost without moving quality.
2. hermes3:8b may be too weak to synthesize regardless of
   harness — the "actually-incapable" possibility task 26 tests.
3. The untested lever: a `planner_alias` split (qwen3 plans
   explicitly "summarize results into bullets", hermes3 follows
   the explicit instruction). That's the task-25 experiment.

**Classifier limitation confirmed:** both modes score identically
on the structural pass-rate — the task-4-noted limitation
(length can't tell a summary from a relay) manifested exactly as
predicted. The comparison requires reading the actual replies.

---

## Flat-loop baseline on daily_news_summary: fetches but relays raw results instead of summarizing

**First observed:** 2026-06-16 (Phase 12 task 4, the flat-loop baseline read).
**Tag:** Phase 12 flat-loop baseline / model-fit / eval classification.

The task-4 read: ran the *current flat loop* (no planning) on the
`daily_news_summary` scenario — "search the web for today's headlines,
then give me a 3-4 bullet summary; push it if you can" — against
hermes3:8b on EC2, 5 samples, via the new `fitt scenario run
fitt-ec2-hermes --mode flat`.

What we saw:

- **5/5 called `web_search` successfully.** No "I can't access
  real-time data" refusal, no answering from stale training data. The
  *fetch* step is solid on hermes3 — the failure is not where the
  spec's running example assumed ("doesn't fetch").
- **0/5 actually summarized.** Every reply relays the raw search
  output — "here are the search results: 1. Fox News — URL: ... Snippet:
  ..." — instead of the requested bullet summary. The capability
  prompt explicitly says "don't just relay a list of links"; hermes3
  ignores it. **This is the real flat-loop failure on this case:
  fetch-then-relay, not fetch-then-synthesize.**
- **`send_message` is unreliable: 1/5 called it** (the prompt said "if
  you can"), and the dev box has no push channel
  (`send_message.no_push_channel`), so even that delivered to a no-op.
- **1/5 went off the rails:** `web_search:ok -> send_message:ok ->
  read_file:error` — after delivering, it made a spurious `read_file`
  call with an unknown project, errored, and narrated the tool error
  back to the user as if answering them.

**Classifier limitation (a finding in itself):**
`scenarios.classify_news_outcome` scored all 5 as `completed` because
the replies clear the 200-char "substantive reply" bar — but they're
link dumps, not summaries. **Reply length cannot distinguish a grounded
summary from a raw-results relay.** Every cheap structural fix (counting
`URL:` / `Snippet:` tokens) is exactly the fragile string-matching the
task-2 conventions say to avoid, so we deliberately did NOT add one.
`completed` therefore means "searched + produced a substantial reply",
NOT "produced a good summary". **Task 22 must read the actual replies,
not just trust the pass rate** — if flat and planned both score
`completed` by length, only reading the text shows whether planning made
it synthesize.

Implication for the planner prompt (Stories 7.1/7.2/7.4): the thing
planning has to fix is the missing *synthesis* step, not the fetch. The
task-22 test is whether an elected plan with an explicit "summarize the
results into bullets" step makes hermes3 actually synthesize rather than
relay.

Tooling: produced by `gateway/scenario_eval.py` + `fitt scenario run`
(the headless multi-sample scenario runner), reusable as-is for the
task-22 flat-vs-planned comparison.

---

## Thinking-model planner stalls: reasoning_content + no tool call reads as "done"

**First observed:** 2026-06-14 (first live orchestrated turn on EC2).
**Tag:** Phase 12 planner pass / agent-loop termination / thinking models.

The first end-to-end orchestrated turn on real models
(`fitt-ec2-hermes`: plan on qwen3:14b via `planner_alias`, execute on
hermes3:8b) ran clean *mechanically* — routing, planner_alias,
executor, web_search, capture all worked — but **no plan was ever
produced**, and the executor ran plan-less (shallow result relay on one
run, a narrated `web_search` JSON-as-text on the next).

Root cause (from the captured cassette, not a guess): qwen3:14b is a
**thinking model**. On the planner pass it emitted **empty `content`,
~1.6k chars of `reasoning_content` (it reasons out the whole plan
in prose), and NO `todowrite` tool call**. `run_agent_loop` terminates
on "no `tool_calls` -> natural stop", so a turn that's empty-content +
reasoning-only + no-tool is indistinguishable from "done": the loop
breaks after iteration 1. The plan never lands in PlanStore.

**`planner_iterations: 2` does NOT fix it** (tested live, hypothesis
disproved): the second iteration never runs, because nothing continues
past a no-tool-call turn. The budget knob only helps a model that
*does* call a tool and needs more round-trips.

So the gap is harness-level, not config:

1. **Planner-level continue-nudge.** When the planner turn returns no
   tool call but has nonzero completion tokens / non-empty
   `reasoning_content` (observable facts, C4-safe), re-prompt once:
   "you reasoned about a plan — now emit it via `todowrite`." This is
   the planner-side analogue of the executor's empty-after-tools nudge.
2. **Possibly carry `reasoning_content` forward** so the model
   continues from its own thinking instead of starting cold on the
   nudge.

Note this is distinct from the 2026-06-11 "tool-blindness" entry below:
that was the planner *refusing on feasibility* (fixed by the
executor-tool hint); this is the planner *thinking but never acting*
under the loop's no-tool-call termination. The tool hint is present
here (the reasoning shows qwen3 correctly planning to use web_search) —
it just never emits the tool call.

Captured fixtures: `~/.fitt/cassettes/ec2-orchestrated-smoke.json`
(budget 1) and `ec2-orch-budget2.json` (budget 2) — both show the
empty-content + reasoning + no-todowrite planner turn. Measured on the
EC2-over-SSM path; a warm qwen3 emitted a single tool call fine on the
boot probe, so this is the planner-prompt/loop interaction, not raw
inability to tool-call.

**Update 2026-06-15 (gemma4:12b-it-qat — the framing was too broad).**
Testing a second thinking model walked this back. gemma4 *mostly
plans fine* (~6-8/10 sampled). Its planner failures are
non-deterministic and not a single "stall":
- **Calls an executor tool from the planner pass.** Caught live:
  gemma4 emitted a `web_search` tool_call (a tool listed only in the
  executor-tools *hint*, not offered in the planner pass) instead of a
  `todowrite`. With budget 1 the loop exhausts with no plan. The
  continue-nudge correctly does NOT fire here (there *was* a tool
  call), so `_is_thinking_stall` returning False is right — but the
  outcome is still no-plan. This is a **side effect of the
  executor-tools hint** (added to stop capable planners refusing): a
  different model reads "here are the execution tools" as "I may call
  them now."
- The qwen3-style empty-content + reasoning + no-tool case also occurs
  occasionally.

Net: the nudge (task 14b) is a **narrow mitigation for one failure
mode on one model (qwen3, n=1)**, not a general fix — "validated live"
was overstated. Characterising planner failure modes per model
belongs in the task-24 capability audit, not ad-hoc onboarding. The
hint's call-the-tool side effect is its own follow-on (the planner
pass arguably shouldn't execute tools it didn't offer).

---

## Planner tool-blindness: capability hint lifts plan-election ~40% -> ~100% (on a capable planner)

**First observed:** 2026-06-11. **Addressed:** 2026-06-12.
**Tag:** Phase 12 task 2.4 / planner pass / eval methodology.

The Phase 12 planner pass offered the model only the `todowrite`
tool. With real models this produced unreliable plan-election:
hermes3:8b and qwen3:14b each emitted a plan only ~2/5 of the time
on a multi-step task ("summarise today's news and send it to me").
qwen3's misses were the tell — it *refused* on feasibility ("I don't
have access to real-time news data or the internet"). The planner
couldn't see that the *execution* step has `web_search`,
`send_message`, etc., so a capable model judged the task impossible
and declined to plan. Prompt micro-tuning didn't help (and a tweak
regressed it); swapping to the bigger model didn't help either — so
it was neither a wording nor a raw-capability problem.

**Fix:** inject the executor's toolset into the planner's system
prompt, framed as "the execution step that carries out your plan has
these tools" (so the model plans steps that *use* them rather than
trying to call them itself). `run_planner_pass` now builds this from
the registry (excluding `todowrite`). qwen3:14b went from 2/5 to
**10/10** plan-election (one run 7/10 with 3 transient empties),
producing clean, tool-grounded plans (web_search -> compile ->
send_message) with clean stops. Validated through the shipped path,
not a prompt hack.

**But it needs a capable planner.** hermes3:8b did NOT benefit
(0/5, n=5): the longer prompt degraded the 8b into emitting the plan
as *JSON text* in the reply or hallucinating a news summary. So the
hint helps a capable planner and hurts the small one. Conclusion:
this is the `planner_alias` lever (design Story 2.2) and the
orchestration-readiness eval dimension (task 24) — **plan with a
capable model (qwen3:14b), execute with the fast one (hermes3:8b)**
(task 25). hermes "feeling better" in daily use is consistent: it's
the strong *executor* (6/6 at direct tool-calling), just not the
planner.

**Eval-methodology note (operator's point).** Inference itself is
flaky — slow models on the EC2/SSM tunnel eat transient timeouts and
empty completions. The eval must categorize each attempt
(PLANNED / NO_PLAN / EMPTY / ERROR-infra) and compute the capability
rate over *valid* attempts, excluding infra/transient failures, and
multi-sample to average noise. The empties here were
non-reproducible (10/10 the very next run) -> transient, not a
capability miss. Caveat: all rates measured on the EC2-over-SSM path
(flaky + slow for qwen3); a stable/home setup will differ, and call
latency correlates with transient-failure exposure (hermes's ~6s
calls dodge what qwen3's ~60s calls catch). The categorization is now
in the multisample harness (`.scratch/run_planner_multisample.py`)
and feeds the task-24 capability profile.

---

## Dev loop was blind to real models; now wired to local + EC2 Ollama

**First observed:** 2026-06-09. **Addressed:** 2026-06-09.
**Tag:** dev-workflow / Phase 12 task 1 (resolved).

Starting Phase 12 (planning/orchestration) surfaced that the
dev/eval loop couldn't exercise a real model: unit tests use
fakes, and the bound models run at home / on EC2, not from the
dev box. For a phase whose correctness *is* real weak-model
behavior (does it plan, does it tool-call under prompt load),
that meant building the prompt-sensitive parts blind — and my
"eval first" instinct was really "I can't see the model and want
to." Diagnosed mid-session (operator caught the framing).

**Addressed** by wiring the existing eval harness
(`fitt eval alias`) to real Ollama backends — no new harness, just
config + reachability:

- A local dev config (`~/.fitt/config.yaml`) pointing at this
  box's Ollama (`qwen3:8b`, `qwen2.5-coder:14b`).
- The EC2 A10G reached over an **SSH local-port-forward**
  (`-L 11435:localhost:11434 ec2-instance-1`) — Ollama stays bound
  to localhost on EC2, no public exposure, no security-group
  change. Pulled `hermes3:8b` + `qwen3:14b` (the home pair) +
  `qwen3:8b`.

First real signal (the payoff):

- `hermes3:8b` (EC2 A10G): **6/6 bare, 6/6 realistic (~970-token
  capability block)**, sub-second/case.
- `qwen2.5-coder:14b` (local): **1/5 bare**, narrated 4/5,
  ~1 min/case — the documented narration failure mode, reproduced
  live with a real model.

**Caveats / open:** the realistic run only reached ~970 tokens
(memory/skills off in the dev config); the documented degradation
was ~5K. A true degradation read needs a full production-size
prompt. Record/replay for deterministic CI (phase12 task 3) is
still to come. Context-tolerance method (declared window as free
bound + measured operating-point + cheap binary-searched probe) is
captured in phase12 task 24.

**Lesson:** get a real model in the loop *before* building
model-sensitive code — the "enabling step, not a baseline ritual"
framing in phase12 task 1. Also: qwen3 is a reasoning model whose
long thinking phase makes `stream:false` calls block a long time
(disable with `think:false` or budget it) — `hermes3:8b` doesn't
think, hence the latency gap.

---

## cron_add couldn't be driven by a small model — and no test could have caught it

**First observed:** 2026-06-08. **Fixed:** 2026-06-08.
**Tag:** tool-schema ergonomics + eval-coverage gap (the
schema half closed; the harness half open).

Asked FITT (Telegram, `fitt-hermes` → `hermes3:8b`) to set a
plain reminder: "remind me to take out the trash at 8pm
tonight." Three `cron_add` calls, all errored, turn gave up.
The turn-detail page told the whole story: call 1 supplied
`message` + `schedule_spec` → `'name' is required`; call 2
supplied `name` + `schedule_spec` → `'message' is required`;
call 3 supplied `message` + `schedule_spec` again → `'name' is
required`. The model oscillated between two of three required
fields and never converged. (Two secondary `hermes3:8b`
weaknesses rode along: it generated past dates — 2022/2023 —
and on the final iteration narrated the tool call as text
instead of emitting a real `tool_call`.)

**Root cause:** `_SCHEMA_CRON_ADD` required three fields —
`name`, `message`, `schedule_spec` — and one of them was
literally named `name`, colliding with the function's own
name. That's a fumble magnet for a small model: three slots to
fill correctly in one shot, with a confusing label on one of
them. `name` was never load-bearing — the cron `id` is the
key; the label is cosmetic and trivially derivable from the
message.

**Fix (schema half, commit cead402):** required reduced to
`[message, schedule_spec]`; `name` made optional and derived
from the message (`_derive_cron_name`) when absent; properties
reordered so the required pair leads; tool description rewritten
to state REQUIRED args explicitly. Regression tests swapped
`test_cron_add_requires_name` for
`test_cron_add_name_optional_derived_from_message` +
`test_cron_add_still_requires_message`.

**Why no test caught it — the real lesson.** There *are*
`cron_add` unit tests, and they passed the whole time. But
every one of them hand-writes a *correct* args dict — they
prove the handler works when given good arguments, which can
never surface a schema that a *model* can't fill. The thing
that should catch this is the eval suite ("can this model emit
the right tool call?"). But `alias_eval.py` /
`alias_eval_coding.py` test **synthetic** tool schemas declared
inline (`read_file`, `grep_repo`, `list_capabilities`, and in
the coding suite `edit_file`/`glob_search`/`shell`) — they
never load the real registry from `build_cron_tools()` /
`build_fileops_tools()`. So the actual `cron_add` schema, with
its fumble-inducing shape, was never put in front of a model by
any test. It only met one in live use.

**The coverage gap, stated plainly:** our eval harness tests
tools we wrote *for the eval*, not the tools we *ship*. Schema-
ergonomics bugs in the real registry are invisible to it by
construction.

**Audit of the rest of the registry (the "other tools?"
question):**

- `edit_file` — **4 required** (`project`, `path`, `old_str`,
  `new_str`), plus `old_str` must match exactly once. Highest
  remaining fumble surface; the next one I'd expect to thrash.
- `write_file` — 3 required (`project`, `path`, `content`).
- **Naming inconsistency across tools:** `cron_add` calls the
  text-to-say `message`; `send_message` and `learn_add` call it
  `text`. The model hit this live (put `message` where `text`
  was expected → `'text' is required`). Three tools, three
  names for "the words" — a fumble cause in its own right.
- Clean (single obvious required arg): `web_search` (`query`),
  `http_get` (`url`), the cron id-only tools, gitops
  (`project`).

**Fix plan (harness half, open):** extend the eval harness to
run the **real registered tools** (not re-declared synthetic
copies) so schema-ergonomics regressions surface in the
dashboard verdict instead of in a live reminder. When that
lands, normalising the `message`/`text` naming and flattening
`edit_file`'s required set become eval-measurable rather than
guesses. Not started — needs an explicit go and probably its
own small spec.

**Urgency:** the schema fix was high (a personal assistant that
can't set a reminder is failing its core promise) and is done.
The harness extension is medium — it's the systemic fix that
keeps this class of bug from recurring on the next tool.

---

## Tool-schema fumble surface across the registry (edit_file, message/text naming)

**First observed:** 2026-06-08 (audit prompted by the
`cron_add` failure above). **Tag:** tool-schema ergonomics,
open. Standalone entry so these don't stay buried in the
cron writeup.

The `cron_add` fix closed one fumble magnet; the registry
audit turned up two more that no model has thrashed on *yet*
but that have the same shape. Pinning them here so they're
findable on a "what's next" scan instead of living inside
another issue's audit section.

1. **`edit_file` has 4 required fields** — `project`, `path`,
   `old_str`, `new_str` — and `old_str` additionally has to
   match the file exactly once. That's the largest required
   set of any inline tool plus a correctness constraint on one
   field. A small model has to land all four in one shot. This
   is the next tool I'd expect to fail the way `cron_add` did.
   Possible eases: derive nothing here (all four are
   load-bearing), but the exactly-once constraint could give a
   more actionable error that quotes the near-miss, and the
   description could lead with the required set the way
   `cron_add`'s now does. `write_file` is the milder sibling
   (3 required: `project`, `path`, `content`).

2. **"The words" has three different names across tools** —
   `cron_add` uses `message`, `send_message` uses `text`,
   `learn_add` uses `text`. The model already tripped on this
   live (supplied `message` to a `text` slot →
   `'text' is required`). Inconsistency taxes every session
   that touches more than one of these tools. Fix is a rename
   to one canonical name, but it's a breaking change to the
   tool contract, so it wants the eval harness covering the
   real registry first (see the cron entry's open follow-up)
   so the rename is regression-checked rather than hand-waved.

**Cost:** latent. No live failure attributed to these two yet
(beyond the one `message`/`text` slip), but they're the same
class of bug as the cron one, which *did* cost a full failed
turn. The point of recording them now is to fix them before
they're the next live incident.

**Dependency:** both are best done *after* the eval harness
exercises the real registered tools, so the changes are
measured against an actual model rather than asserted safe.
Until then this is a watch-list, not a work item.

---

## Probe flattened "slow / cold-loading" into "transport_error" on a shared-GPU laptop

**First observed:** 2026-05-28. **Fixed:** 2026-06-02
(Phase 7.6). **Tag:** observability / correctness (closed).

Re-probing three aliases (qwen3:14b, hermes3:8b, granite3.3:8b)
that all point at one laptop's Ollama on a 12GB GPU returned
"1 of 3 ok, 2 transport_error". The two failures weren't broken
models — the three probes fired **concurrently** (the old
`probe_all_aliases` used a flat `asyncio.gather` with a "no
contention across aliases" docstring that was false for the
dominant FITT shape), fought over VRAM, and two blew past the
10s timeout while cold-loading. Worse, the failure label was
`transport_error`, which reads like "can't reach the host" —
the exact opposite of the truth (the host was fine, the model
was loading).

**Cost:** Misleading. The operator can't tell "my laptop is
asleep" from "the model is slow" from "the binding narrates
instead of tool-calling" when everything collapses to one word.
Drove a debugging session chasing a network problem that didn't
exist.

**Root cause:** two compounding issues. (1) Vocabulary
fragmentation — the chat path had a mature failure taxonomy
(`upstream_silent` / `upstream_rate_limited` / ...) while the
probe and eval flattened everything to `transport_error`. (2)
Self-inflicted contention — concurrent probes on one GPU
serialise model loads, so probes behind the first time out.

**Fix (Phase 7.6, spec `phase7.6-probe-clarity`):**

- Shared dispatch-outcome taxonomy (`gateway/dispatch_outcome.py`)
  — one vocabulary across chat / probe / eval. `transport_error`
  is gone.
- Reachability-on-timeout: a timed-out canary runs the same
  cheap ping `/ready` uses (`gateway/reachability.py`) and
  reports `upstream_silent` (reachable — slow / cold-loading)
  vs `unreachable` (host down).
- Sequential same-endpoint probing: aliases sharing an endpoint
  probe one at a time; distinct endpoints still overlap.
- Per-probe latency, an amber/red pip split (environmental vs
  broken binding), an endpoint column, and a unified per-alias
  dashboard page (`/dashboard/alias/<id>`) that puts config,
  the shared-GPU "shares with" line, probe detail, and the
  eval suites in one place.

**Urgency at the time:** medium — not a functional outage, but
it actively misled debugging. Closed by living with Phase 7 for
a day (Principle 9) and shipping the follow-up.

---

## Phase 7 live-validation pass — markdown rendering on command outputs

**First observed:** 2026-05-28. **Fixed:** 2026-05-28.
**Tag:** UX (closed), Slice 7.4 follow-up. Caught during the
phase-closeout live-validation pass before flipping the DONE
flag.

The `/model` Telegram command rendered `*Aliases:*` as
literal asterisks on the phone, and the growing turn bubble
showed `Ran \`web_search\`` with literal backticks. Slice
7.4 (Phase 7 markdown renderer) had landed for the streaming
path and turn-bubble flushes (tasks 19a/b), but the deferred
19c (approval prompts) and 19d (command response
constructors) hadn't been touched — and the bubble's
task-line assembly was calling `html.escape` instead of
`markdown_to_telegram_html`, preserving backticks instead of
converting them to `<code>` tags.

**Cost:** Cosmetic, not functional. Replies still made
sense; they just looked unfinished. Caught during the
2026-05-28 validation pass that was supposed to confirm
Slice 7.4's "phone renders correctly" property.

**Fix landed in commit 460f1d4.** Three changes: (1)
`turn_renderer._render_stream_bubble` routes task lines
through `markdown_to_telegram_html`; (2) `handle_session_command`
and `handle_model_command` route their composed markdown
through the renderer with `parse_mode="HTML"`; (3)
`_KNOWN_TOOL_VERBS` registers `web_search`,
`list_capabilities`, `grep_repo`, `glob_search`,
`list_directory`, `http_get` — clean verb pairs instead of
the generic fallback. Five regression tests in the telegram-
bot suite pin each fix.

19c (approval prompts) stays deferred until LLM content
surfaces in approval bubbles. The pre-existing `_format_eval_summary`
/ `_format_status` / `_format_lastturn` already compose HTML
directly, so they were unaffected by the gap.

**Lesson:** "Live-validation pass" caught a real issue the
in-process unit tests couldn't have. The shape of the
issue was a Slice 7.4 deferral that became wrong once
Slice 7.3's commands shipped — exactly the boundary the
deferral was waiting on. Worth respecting "deferred until
X" lists during phase-closeout passes; the deferral
condition often resolved during the phase itself.

---

## Skills property test flaky on hypothesis "no" alphabet

**First observed:** 2026-05-22 during Phase 7/7.2 development.
**Tag:** flaky test, low pain.

`tests/test_skills_properties.py::test_property_scan_failure_isolation`
fails reproducibly when hypothesis generates a skill named
`"no"`. The skills loader rejects the skill (logged
`skills.skipped`) even though the SKILL.md is well-formed. The
test asserts every valid name lands; the rejection breaks the
assertion.

Reproduces on clean `main` without any of Slice 7.2's changes,
so this is a pre-existing skills-loader bug surfaced by
hypothesis's seeded shrinking, not a Slice 7.2 regression.

**Cost:** Low. The full test suite passes ~99% of the time;
only when hypothesis happens to seed the `"no"` example does
it fail. CI will fail intermittently; local devs may not see
it for weeks.

**Fix plan:** investigate why the skills loader rejects a
skill named `"no"`. Likely a `bool(name)` vs
`name == "no"` mistake in the skill validator. Half hour to
locate; another half hour to fix and add a regression test.
Not blocking Phase 7.

---

## Granite 3.3 narrates tool calls under FITT's full system prompt

**First observed:** 2026-05-22. **Tag:** model-fit, medium pain.
Cross-references `docs/choosing-a-model.md` (system-prompt-size
as a model-fit axis) and motivates the Phase 7 visibility work.

`granite3.3:8b` is bound to `fitt-default` on Ollama. Direct
hit against `localhost:11434/api/chat` with a single tool +
no system prompt: clean structured `tool_calls` response,
141-token prompt, 15-token completion, ~2s. Hit through FITT's
gateway with FITT's full system prompt (capability block,
identity, lessons, skills, no history yet — fresh session):
narration in YAML/JSON shape inside `message.content`, no
`tool_calls` field, 5405-token prompt, 103-token completion.
Same model, same Ollama backend, same wire format. The only
load-bearing variable: prompt size, ~38× larger.

**The router-mode (`X-FITT-Client: coding-agent`) test pinned
it.** Through the gateway with FITT's prompt-injection bypassed
and a single user-supplied tool: clean `tool_calls`, 159-token
prompt. So the model is fine; FITT's system prompt is what
flips it.

**Cost:** every Telegram tool-use turn against this binding
narrates instead of dispatching. The agent loop sees no real
`tool_calls` and treats the narrated text as the assistant's
final reply. The user gets a model claiming it ran a tool with
no actual execution — exactly the Problem C (self-deception)
shape the hallucinations doc warned about, surfaced via a
different failure path (model-fit, not regex-narration).

The boot probe (`alias_probe`) didn't catch this because it
fires a 159-token canary; the model passes there. Same data
shape Phase 7's realistic-prompt eval flag is meant to surface.
Same data shape no eval today reports.

**Root cause framing.** Models advertised as "supports tool
calling" pass abstract benchmarks at minimal prompt size.
Discipline degrades with scale. Smaller models (≤12B) lose
structured-output adherence faster than larger ones — the
post-training that teaches "emit `tool_calls`" is fighting the
post-training that teaches "follow long system prompts." The
literature (see `docs/hallucinations-and-poisoning.md` on the
"lost in the middle" effect) frames this as context-window
degradation; for FITT's purposes it shows up well before the
window's ceiling, around 4-6K tokens of system prompt for
8B-class models. The choosing-a-model doc treats this as the
operator-controllable knob; this entry is the concrete
incident.

**Mitigations, ordered.**

1. **Route `fitt-default` to a model that handles long prompts
   with tools.** Per the choosing-a-model doc, `qwen3:14b` /
   `llama3.1:8b-instruct` / `mistral-nemo:12b` are documented
   to handle multi-thousand-token system prompts cleanly; cloud
   models (Claude Haiku, GPT-4o-mini) handle them at any size.
   This is the right answer when reliable tool calling matters
   and is what a future Phase-7-informed binding decision should
   default to.
2. **Phase 7 surfaces this failure mode by default.** The
   per-turn traceability capture (Slice 7.2) logs the
   `prompt_tokens`, `context_window`, and `prompt_pct_of_window`
   for every turn. The Telegram `/model` command surfaces the
   same. An operator hits this case again, sees "5405 tokens on
   a 32k-window model, narrated, finish_reason=stop, no
   tool_calls," and the diagnosis is a glance instead of an
   evening.
3. **Realistic-prompt eval** (deferred to Phase 7+ opportunistic).
   `fitt eval alias <name> --realistic` runs the eval suite with
   FITT's actual injected prompt rather than the bare canary.
   The diff between bare and realistic runs is the diagnostic.
4. **Compact-prompt mode for small models** (Phase 7+
   opportunistic). `tools.compact_capability_block: true` skips
   the prose trailer in the capability block and renders only
   the tool list. Bandage for binding to small models without
   a swap.

**Note on Ollama `num_ctx`.** Operator had set
`OLLAMA_CONTEXT_LENGTH=256k` (the maximum granite supports), so
the prompt reached the model intact rather than being silently
truncated at the default 2048. This *isn't* the bug — but it's
the discoverability gap Phase 7's context-awareness slice
(7.1) addresses: FITT today has no awareness of whether `num_ctx`
is at default, at the operator's override, or at the
architecture ceiling. Without that, compaction (Phase 8) can't
know when to fire.

**Observation worth pinning:** the bug was diagnosed in roughly
two hours of conversation that involved reading source for
`chat.py`, `agent_loop.py`, `router.py`, `capabilities.py`, and
`alias_probe.py`, plus three direct curl tests, plus
ssh-into-container. Phase 7's whole reason for existing is to
turn that two-hour debugging session into a 30-second
dashboard glance plus a `/model` command. The work is
load-bearing for the project's "I'm a programmer, I want to see
what goes wrong" posture (project lead, 2026-05-22).

---

## Narration shape-check fired on every chit-chat turn

**First observed:** 2026-05-12. **Rolled back:** 2026-05-12.
**Tag:** design (closed by removal), sibling of the claim-check
rollback landed the same day.

`is_tool_use_expected_but_none` is a shape-level classifier:
tools were offered + clean finish + no `tool_calls` + reply
over 40 chars → "model declined to call a tool when one was
expected." Shipped 2026-05-11 as a runtime signal emitted by
`record_narrated_tool_call` from the chat tool-loop and cron
firings. The doc
`docs/hallucinations-and-poisoning.md` framed the original
signal with the precondition *"the user's original message
triggered tool-calling expectations"*; the implementation
dropped that precondition because no cheap honest signal
exists for user intent.

Live Telegram session 2026-05-12 produced three
`tool_call_narrated` events in one short conversation:

- `"I'm ready to help! Could you clarify..."` (no prior
  context)
- `"You're welcome! Let me know..."` (reply to "Thanks")
- `"I'm FITT, your personal AI assistant..."` (reply to
  "Who are you")

All three were correct model behaviour. None of them
involved a user asking for an action. The signal fired at
100% on ordinary chit-chat because the Telegram bot
always loads FITT's tool registry into the request, which
the shape check reads as "tools were offered."

**Cost:** The same as claim_check: noisy events train the
operator to ignore the signal, which was meant to surface
genuine tool-call failures. Every Telegram conversation
with casual messages was a false-positive generator.

**Root cause (lesson):** The doc's precondition was the
load-bearing part. Shipping a shape signal without it was
the same anti-pattern as shipping a regex for hallucination
detection: trying to infer user intent on the cheap.

**Fix:** Removed `record_narrated_tool_call` from
`agent_loop.py`, the `detect_narrated_tool_call` detector +
`NarratedToolCall` dataclass + `_NARRATED_TOOL_RE` regex from
`capabilities.py`, the callers in `chat.py` and
`cron_runner.py`, the `tool_call_narrated` event kind from
the CLI color map, the e2e lifecycle test, and the narration
assertions from `test_cron_runner.py`. Every doc / spec /
roadmap reference to the runtime event kind updated.

**What's still real:** `is_tool_use_expected_but_none` stays
as a pure classifier used by `gateway.alias_probe` (boot-time
canary) and `gateway.alias_eval` (on-demand harness). Those
two contexts supply the expected-outcome precondition by
construction — the test author wrote the case. That's where
the signal belongs.

**Rule for future signals:** base decisions on ground truth,
not on flimsy inference of intent. If the cheap signal
requires regex on content, keyword heuristics, or the shape
of the model's reply to decide whether the user wanted an
action, don't ship it in live chat. Put it in the eval
harness where the precondition is pinned, or don't ship it
at all.

---

## Receipt cross-check regex captured "a" as a tool name

**First observed:** 2026-05-12. **Rolled back:** 2026-05-12.
**Tag:** bug (closed by removal), Principle-3 / own-doc
violation.

The `claim_check.py` module shipped 2026-05-11 as
"minimum-viable receipt cross-checking" for Problem C. The
live Telegram session on 2026-05-12 surfaced exactly the
failure mode `docs/hallucinations-and-poisoning.md` had
explicitly warned against: the regex captured `"a"` as a
tool name from the chatty phrase *"using a secure,
privacy-first toolset"*, firing a `tool_claim_mismatch`
event on benign natural-language text.

**Cost:** Every chatty Telegram reply that mentions
"using", "via", or "I used" in passing was a false-positive
candidate. One event per session-with-prose was typical.
More insidious than the event noise: the signal trained the
operator to ignore `tool_claim_mismatch`, which was meant to
flag the actual Problem C failure mode.

**Root cause:** I latched onto the word "receipt" in the
hallucinations doc's item 3 and shipped a regex claim
parser despite the same doc's explicit not-list ("regex
matching on any specific hallucination shape") pointing
straight at it. The "lexical-signal version" framing in the
commit message was wallpaper, not a rationale.

**Fix:** Removed `gateway/src/gateway/claim_check.py`,
`gateway/tests/test_claim_check.py`,
`agent_loop.py::record_claim_mismatch`, the chat + cron
callers, the `tool_claim_mismatch` event kind, and every
doc / spec / roadmap reference. The audit log at
`$FITT_HOME/audit.jsonl` remains the real receipt layer —
tamper-evident and authoritative. An operator checking
`fitt inbox` / `fitt audit tail` when something feels off
is the only reliable cross-check we have. There is no
Phase 2 in the queue; an "LLM-based claim extractor"
parses the same prose the regex did, just more
expensively, so it's the same anti-pattern. When the
user-facing experience of Problem C hurts enough to
revisit, the right conversation is with fresh eyes, not
a plan stashed here.

**Lesson for the agent:** if the backing doc lists the
approach you're about to take on its explicit don't-do
list, the right answer is to not do it, not to rebrand it
as a minimum-viable starter. The doc exists to prevent
exactly this failure mode.

---

## 🔓 Trust session button did nothing

**First observed:** 2026-05-11. **Fixed:** 2026-05-11.
**Tag:** bug (closed), high pain. Principle 8 gap.

Every Telegram approval prompt rendered three buttons:
✅ Approve, ❌ Reject, 🔓 Trust session. Tapping 🔓 correctly
routed the click to the gateway's decide endpoint, which
called `ApprovalMiddleware.trust_session(session_key,
tool_name)` as designed. The method body was a documented
no-op: a single `_log.debug("approval.trust_session.noop",
...)` and nothing else — Task 8c, deferred at Phase 4 shipping
time and never completed. The next tool call in the same
session re-prompted identically to how Approve would have
behaved.

**Cost:** Every multi-step Telegram coding session paid
N taps for N tool calls. Observed during live use on
2026-05-11: three `edit_file` prompts for one turn's work,
with the operator tapping "🔓 Trust session" on the first
one and being confused when the second still asked. A
classic Principle 8 gap — the UI promised session-level
trust, the backend silently didn't deliver.

**Fix:** `ApprovalMiddleware` gained
`_trusted: dict[str, set[str]]` (session_key → trusted tool
names). `trust_session()` writes to it. `check()` gained a
short-circuit after the deny-list check and the early
auto/block/yolo branches: if the session already trusts the
tool, return `ApprovalDecision.trust_session(detail=
"previously trusted for this session")` without creating a
pending approval. `clear_session()` drops the session's
trust set so CLI archive / delete paths stay clean. Trust
is per-(session, tool); it does NOT bypass the deny list
(which runs first); it does NOT survive a gateway restart
(persistent trust graduates to config.yaml's
`bucket=auto`). 8 tests cover the short-circuit path,
cross-session isolation, deny-list precedence, per-tool
scope, restart behaviour, `clear_session`, and the
end-to-end decide-handler flow the Telegram bot uses.

---

## FITT capability block leaks into coding-agent clients (Aider)

**First observed:** 2026-05-11. **Fixed:** 2026-05-11.
**Tag:** design (closed), medium pain. Cross-references the
Phase 4 "tool forwarding, not replacement" decision and the
prompt-injection concerns in Phase 4.7's threat model.

Pointed Aider at FITT as its model backend. Aider's own
system prompt asked something shaped like "what tools do you
have?" FITT answered with its own capability block — the
gateway-side `list_capabilities` / inline tool descriptions
— not with what Aider actually has. The inside-Aider session
then spent its first turn calling `list_capabilities`, got
FITT's tools back, and tried to reconcile two completely
separate agent frameworks in one conversation.

This is the Mode 1 / Mode 2 collision in the open. FITT wants
to be a hub that layers memory + tools + approvals on top of
the model (the Telegram case). Aider is itself a coding agent
that owns its own loop, prompt, tools, diff workflow, and
commit discipline. When Aider treats FITT as "just an
OpenAI-compatible endpoint," any FITT-side injection —
capability block in the system prompt, FITT tools merged
into the request's `tools` array, memory snippets prepended
— actively confuses Aider's own agent.

**Cost:** Proportional to how much the author wants to use
FITT-as-router for coding-agent tools (Aider today; Claude
Code, Cursor, Continue-Agent, Codex, Kiro-CLI tomorrow). At
minimum: one wasted turn per session chasing a ghost tool
list. Worst case: the model pattern-matches on FITT's `ssh`-
routed file tools and tries to call them instead of Aider's
own file edits, which silently breaks the Aider workflow.

**Fix plan:** Router-mode for coding-agent clients. Classify
clients via `X-FITT-Client` (values `aider`, `claude-code`,
`cursor`, `codex`, or the generic `coding-agent`). When the
client is in router mode: skip capability-block injection,
skip FITT tool merge into the `tools` array, skip memory
injection, skip approval middleware (the client owns that
surface). Keep: alias resolution, backend dispatch, cost
tracking, and audit-log entry for model usage. Preserve
today's "agent mode" for Telegram / Open WebUI / raw curl
where FITT's layered value is exactly what's wanted.

Default for unclassified clients stays "agent mode" — safer
toward visibility than silently stripping everything.

Work sits in `gateway/src/gateway/chat.py` at `_inject_memory`,
`_inject_fitt_tools`, and the capability-block check around
line 770. One mode-enum, three gates. Tests prove router-mode
requests pass through cleanly.

This is the concrete answer to the "how much does the coding
framework interfere when FITT is used in an IDE or CLI" open
question. Router mode for known coding agents; agent mode
for everything else.

**Fix landed 2026-05-11:** A new `coding-agent` client tag joins
`{ide, telegram, webui, cli}` as an accepted value for
`X-FITT-Client` and the `client:` field on tokens. Single
source of truth lives at `gateway.auth.is_router_mode_client()`;
`chat.py`'s chat handler calls it once at request entry and
branches:

* Router mode (`coding-agent`): skip memory load, skip
  capability-block construction, skip `_inject_memory`, skip
  `_inject_fitt_tools`, and skip the FITT tool loop
  altogether. The request body reaches LiteLLM as the client
  sent it (minus the client's concrete `model` field, replaced
  by the alias's backend model id — that's the whole point).
  Approval middleware isn't consulted because no FITT tool
  runs.
* Agent mode (everything else — today's behaviour): unchanged.

What FITT still does for router-mode clients: alias resolution
(`fitt-smart` → the configured backend), dispatch via
LiteLLM, cost tracking, audit-log entry for the model call,
fallback handling, `X-FITT-Backend` header. What the client's
own agent owns: system prompt, tool schemas, tool execution,
approval UX, memory.

Default for unclassified clients stays `webui` (from the auth
middleware's token resolution), which is NOT router mode —
safer toward visibility than silently stripping every FITT
feature for a client that hasn't opted in. 9 tests pin the
no-system-message, no-FITT-tools-merged, no-memory-leak,
no-FITT-tool-loop, still-resolves-aliases,
still-rejects-concrete-model-ids contract, plus the Telegram
regression guard and the unclassified-client default.

Operator setup for Aider: add
`X-FITT-Client: coding-agent` to Aider's `extra_headers` config,
or tag the Aider token with `client: coding-agent` in
`secrets.yaml`.

---

## Silent failure when api_keys entry is missing for an openai-backend model

**First observed:** 2026-05-11.
**Partially fixed:** 2026-05-11 (boot-time ERROR log; the
LiteLLM runtime failure is unchanged).
**Tag:** design, Principle 11 (closed).

Adding a new `openai`-backend model (e.g. a new NVIDIA NIM
binding) requires two coordinated edits: `config.yaml` gets
the `models:` entry + alias pointer, and `secrets.yaml` gets
an `api_keys.<model.id>` entry. If the `api_keys` entry is
missing or keyed on the wrong name, the gateway starts
cleanly with no warning. The first time the alias is
dispatched, LiteLLM's router can't find an api_key, falls
back to its default OpenAI client, and raises
`litellm.AuthenticationError: the api_key client option
must be set either by passing api_key to the client or by
setting OPENAI_API_KEY env variable`.

The error message is correct but misleading: the fix isn't
to set `OPENAI_API_KEY`, it's to add the matching
`api_keys` entry in `secrets.yaml`. An operator seeing
this for the first time will reasonably try the obvious
thing and end up confused.

**Cost:** Low in absolute terms (minutes of confusion per
incident) but it's a Principle 11 violation — the
misconfiguration is detectable at boot and we're not
surfacing it. Every new model binding is a fresh
opportunity to hit it.

**Related gotcha worth naming:** `api_keys` is keyed on
the model's `id` field, not on the alias name. Several
aliases can point at the same model id and share a key.
Easy to assume otherwise when staring at `aliases:` and
`api_keys:` side by side.

**Fix plan:** Add a boot-time pass in config load (likely
`config.py` or `app.py` startup) that walks every model
with `backend: openai`, verifies `secrets.api_keys.<id>`
exists, and logs an ERROR with the exact
`api_keys` entry to add when it doesn't. Don't refuse to
start — other aliases might still work — but make the
misconfiguration unmissable in the logs.

Shape:

```
ERROR config.secrets.missing_api_key
  model_id=nvidia-qwen3-coder
  fix="add `api_keys: { nvidia-qwen3-coder: nvapi-... }` to secrets.yaml"
```

Worth bundling with the second Principle 11 item: a
boot-time tool-call reliability probe per alias (in the
hallucinations doc's action list). Both have the same
detect-at-boot-warn-loudly shape. If we do one we should
consider doing the other in the same session.

Hours of work. Not blocking but shouldn't sit forever.

**Fix landed 2026-05-11:** `gateway/src/gateway/config.py`
gained `check_missing_api_keys(config)` which returns a
list of human-readable warnings for openai-backend models
whose `api_keys` entry is missing. `app.py`'s `create_app`
calls it at startup and emits an ERROR log line per
warning. Non-fatal — other aliases still work. Tests in
`test_config_boot_checks.py` cover happy path, missing key,
key-name-mismatch (the exact mistake in the incident),
mixed backends, multiple gaps, and the secrets-not-loaded
CLI case.

The runtime LiteLLM failure with its misleading
"OPENAI_API_KEY not set" message is unchanged — we can't
intercept that without a much bigger middleware
intervention — but now the operator sees the real cause
in the gateway logs at startup before the misleading
runtime error lands. That's the Principle 11 property we
wanted.

The sibling Principle 11 item — boot-time tool-call
reliability probe per alias — is deferred. It needs real
LLM dispatch at startup (network, token cost, timeout
handling) and is bigger than this half-day item.

**Fix landed 2026-05-11 (both halves of Principle 11
backlog).** The tool-call reliability probe shipped as
`gateway/src/gateway/alias_probe.py`: a canary request per
alias at startup with a synthetic `_fitt_probe` tool in the
`tools` array, shape-level classification of the response,
one ERROR log per narrated / truncated / transport-failed
alias. Would have caught the 2026-05-07 qwen2.5-coder
narration and the 2026-05-10 qwen3-next sentinel pattern on
the first gateway boot instead of on the first live
Telegram turn. Sized to the same half-day bucket as the
api_keys check thanks to the `extract_tool_calls` helper
from `agent_loop.py` being reusable. Disabled via
`server.boot_probe_enabled = false` in tests; 10s default
timeout configurable via `server.boot_probe_timeout_s`.

---

## `_persisted_args` serialization leak poisons tool-call history

**First observed:** 2026-05-10 (Telegram coding session).
**Fixed:** 2026-05-11.
**Tag:** bug (closed), high pain. Cross-references Problem B
in hallucinations doc.

Tool calls in persisted history showed up as
`http_get(_persisted_args="url='https://wttr.in/...'")`.
That's not an OpenAI tool_call shape. `_persisted_args` was
a gateway-internal placeholder added by the history reader
when it couldn't invert the pretty-printed args summary
back into a real structured dict. Once one turn persisted
with this shape, every subsequent turn's model saw the
pattern in its loaded history and mirrored it — producing
tool calls with `_persisted_args=` as the argument name
instead of the real argument names. The tool handler
rejected them with "Missing required argument: project."
The model got confused by its own errors and fell back to
the gap-reporter ("I'd need a tool to read a file") for
tools that were literally in its capability list.

**Cost:** From the 2026-05-10 session, roughly 40% of tool
calls failed on argument names from the moment the leak
started, and the model visibly got worse at recovery as
the session dragged on. This single bug cut the session's
usefulness in half.

**Root cause:** The on-disk format stored args as a lossy
summary string (`project='hub', command='ls'`, truncated
at 80 chars). The reader then had to reconstruct an
OpenAI-shape `tool_calls` dict from that summary, which
isn't possible — the summary is lossy and ambiguous. The
reader's workaround was to stuff the un-parseable text
into a `_persisted_args` placeholder key.

**Fix:** Changed the on-disk format to store the real
structured args as a fenced JSON block alongside the
human-readable bullet. Reader reads the JSON directly. No
parser needed on the summary. `_persisted_args` key
deleted. Tests updated to pin byte-accurate round-trip
(the property the old design couldn't give us).

**Operator action:** The fix is not backwards-compatible
with history files in the old format. If you have any
`.md` files under `$FITT_HOME/sessions/<session>/history/`
written before the fix, the reader will now raise loudly
on load with a message pointing here. Clear them:

```bash
rm -rf $FITT_HOME/sessions/*/history
```

History files for chat-only sessions (no tool calls) load
identically across the change and don't need clearing.
Only files containing `## <ts> assistant tool_calls`
headers are affected. If you're not sure, check with:

```bash
grep -l 'assistant tool_calls' $FITT_HOME/sessions/*/history/*.md
```

If no files match, nothing to clear.

---

## Gap-reporter false positives cascade

**First observed:** 2026-05-10. **Tag:** design, medium pain.

The capability-gap reporter was designed to catch the
"I'd need a tool to X" phrasing when the model asks for a
capability it doesn't have, appending to
`$FITT_HOME/capability_gaps.log` as a natural backlog. In
practice, once tool calls start failing on argument errors
(see `_persisted_args` above, or any other source of tool
errors), the model falls back to the gap-reporter phrasing
for tools it *does* have. The log then fills with false
positives: "I'd need a tool to read a file" for
`read_file`, "I'd need a tool to edit a file" for
`edit_file`.

**Cost:** The capability-gap log becomes untrustworthy as a
next-tool backlog, which was its whole point. Operator has
no easy way to tell real gaps from tool-error-cascade false
positives.

**Fix plan:** Suppress gap-log writes when the tool the model
is asking for is actually registered. Cheapest version: check
`registry.has(tool_name)` before appending; if the tool
exists, log to a separate `capability_gap_false_positive.log`
or just the regular application log for diagnosis. Low risk,
an hour of work; blocked mainly on deciding whether the
false-positive stream is worth keeping separately or just
dropping.

---

## Capability false-negative ("I can't provide weather forecasts")

**First observed:** 2026-05-10, minute 1:34 of the session.
**Tag:** design, hallucinations Problem A adjacent.

Model refuses a capability it has. User asks "Is it going to
rain tomorrow?" Model replies "I can't provide weather
forecasts. For accurate predictions, I recommend checking..."
despite `http_get` being in its capability block at that
moment. Took three follow-up messages ("You have tools to
search internet", "Check your tools", "Show me the tools you
have") before the model actually consulted its own
capabilities and found `http_get`.

**Cost:** The capability block exists specifically to prevent
this (Principle 8: the agent is honest about its
capabilities). When the model pattern-matches on "weather"
and refuses before reading its capability block, the block
isn't doing its job. Not a catastrophic failure, but it's
exactly the "silently produces a lesser answer when a tool
would have given a better one" bug the principle forbids.

**Fix plan:** Model-level, so no mechanical fix. Things to
try:

- Restructure the capability block so it reads as "here's
  what you CAN do" rather than a list below an unrelated
  system prompt.
- Add an explicit pre-hook: if the user's message mentions
  a domain the agent has a tool for (web, file system,
  git, etc.), gently remind the model.
- Eval harness (see hallucinations doc) should cover this
  shape: "ask about the weather → model should call
  `http_get`, not refuse."

**Update 2026-06-03 — recurred (Roland Garros), then fixed.**
Same shape, different domain: asked `fitt-hermes` for "today's
Roland Garros match results"; it refused with "my capabilities
don't allow direct access to real-time data" despite
`web_search` being live. Pointing at the tool explicitly made
it search (so the wiring is fine; it's the proactive judgment
that fails). Two-part fix landed:

1. **Made it measurable.** New `live_fact_web_search` case in
   the eval *realistic* suite (`realistic_cases()`): a
   time-varying question with `web_search` offered, expecting
   the call. A refusal scores `narrated` → red verdict on the
   per-alias page. Kept out of the bare default suite on
   purpose — the prompt-sensitive case belongs only in the
   suite that runs under FITT's live prompt, so the before/
   after is a clean A/B.
2. **Prompt nudge (always-on).** New `[Using tools for current
   facts]` section in the capability-block trailer
   (`capabilities.py`), borrowing the enumerate-the-must-use-a-
   tool-categories shape that **both** Hermes
   (`OPENAI_MODEL_EXECUTION_GUIDANCE` `<mandatory_tool_use>`:
   "Current facts (weather, news, versions) → use web_search")
   and OpenClaw (execution-bias "mutable facts need live
   checks") independently landed on. Names web_search for live
   facts, reframes "you are not limited to training data when a
   tool can fetch the answer", and adds Hermes' retry-on-thin-
   results line to fight the link-dump-instead-of-answer
   symptom.

Caveat (unchanged): prompting reduces the rate, doesn't
eliminate it on an undersized model. Model choice is the real
lever — Hermes' own enforcement list (`gpt`, `gemini`, `qwen`,
`deepseek`, ...) notably excludes the Hermes model family,
implying it tool-calls well natively; the families FITT runs
locally are exactly the ones that need the steering. Validate
per-binding with the realistic eval before trusting it.

---

## Cheerleading / success theater in replies

**First observed:** across multiple sessions; acute on 2026-05-10.
**Tag:** prompting, medium pain. Makes hallucinations
Problem C harder to spot.

Every turn on 2026-05-10 ended with some variation of "You
now have a fully tested, production-grade tool!" or "Perfect,
the test file has been successfully created" regardless of
whether anything actually worked. This is performative
success rather than honest reporting.

**Cost:** Self-deception (Problem C) gets camouflaged. A
failed turn that *announces itself as failed* lets the user
course-correct immediately. A failed turn that announces
itself as a triumphant success needs the user to
independently verify, which in practice rarely happens.

**Fix plan:** Prompting-only change. Add to the capability
block or system prefix: *"Report what actually happened,
including failures. Do not frame incomplete work as complete.
No victory laps."* The research (see hallucinations doc's
Feedback Loops citation) says prompting alone doesn't
eliminate this behavior, but it reduces magnitude, and it's
free to try. Minutes of work.

---

## Telegram: approval prompt floats between messages after decision

**First observed:** 2026-05-08, Phase 4.7 validation.
**Tag:** UX, low urgency. (Migrated from
`FITT_ROADMAP.md`'s UX backlog.)

The inline-keyboard approval message stays at its original
chat position after the user decides — the natural-language
reply and the `tool_executed` push both land below it, and
the (now-decided) approval message sits between them. Not
broken (buttons correctly clear; the V-Approved text
replaces them), just a cosmetic "ordering reads weird on a
phone" moment.

**Fix plan:** Delete the approval message after decision
rather than edit it in place. Revisit if it becomes annoying
in practice.

---

## Telegram: double-message for interactive project_shell calls

**First observed:** 2026-05-08. **Tag:** UX, low urgency.
(Migrated from `FITT_ROADMAP.md`'s UX backlog.)

Every approved `project_shell` invocation produces two new
Telegram messages: the model's natural-language reply AND
the `tool_executed` event. Redundant for the interactive
case; useful for `trust_session` / cron firings where there's
no model reply.

**Fix plan:** A config knob
(`tool_executed.suppress_on_interactive` or similar) that
collapses the pair when the chat turn is the one that
triggered the tool call. Phase 4.7+ hardening, not
blocking.

---

## How to add entries

Paste a new entry at the top with today's date. Short slug
heading, tag line, one or two paragraphs of narrative,
optional "fix plan." Link to related docs or specs where
the issue will actually get resolved.

Don't bother with triage fields (priority, status, owner) —
this isn't a tracker. If an entry becomes urgent enough to
track formally, promote it to a spec under
`.kiro/specs/phase<N>-<name>/` or to `FITT_ROADMAP.md`.

Delete entries that stop mattering. A long stale list is
worse than a short honest one.
