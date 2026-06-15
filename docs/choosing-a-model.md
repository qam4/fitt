# Choosing a Model for FITT

How we reason about which model to bind a FITT alias to. Not a
snapshot of current recommendations (those go stale in weeks);
a process you can re-run when the landscape shifts.

Scope: picking backends for `fitt-smart`, `fitt-default`,
`fitt-fast`, or any new alias. Mostly about open-weight models
accessible via NVIDIA NIM, OpenRouter, Ollama, or local
inference. FITT's constraint is no-subscription, so we're not
choosing between Claude / GPT / Gemini tiers — we're choosing
among the open-weight ecosystem and its hosted access points.

## Why this matters more for FITT than for most agents

Frontier proprietary models hide a lot of reliability problems
from their users. When Claude Sonnet mildly drifts on tool-call
format, the post-training is strong enough that it mostly still
works. Users of those models don't develop strong opinions
about tool-call reliability because they don't have to.

Open-weight models span a huge quality range, and their tool-
calling behaviour is the axis that varies most dramatically.
Some models never drift; some models invent their own format
and the OpenAI-compatible wrapper breaks in subtle ways; some
models were trained for tool use and some were fine-tuned on
tool-use-shaped data after the fact with poor results. The
difference between a good choice and a bad one is the
difference between FITT feeling usable and feeling broken.

The 2026-05-08 sentinel incident on `main` (documented in
[`docs/hallucinations-and-poisoning.md`](./hallucinations-and-poisoning.md))
and the 2026-05-10 Telegram session are both downstream of a
poor model choice — picking `qwen3-next-80b-a3b-instruct` for
`fitt-smart` without checking its tool-call reliability
specifically.

## What FITT actually needs from a model

Ordered by how much weight to give each.

### 1. Native OpenAI-style `tool_calls` emission

The gateway's tool loop expects the model to emit
`tool_calls` as a structured field in the response, following
the OpenAI chat completions shape. The field is either present
with a real structure or absent. Anything in between — the
model inventing a function-call shape in `content`, emitting
custom fence formats, wrapping args in its own JSON key — is
what we've been calling "narration" and it's the worst failure
mode we've seen.

When reading a model card, look for specific language:

- ✅ "Supports OpenAI-compatible function calling" — direct
  match.
- ✅ "Supports `tools` and `tool_choice` parameters" — same
  thing, described at the API level.
- ⚠️ "Supports function calling" without qualifier — check
  further. Some models do OpenAI-style, some emit their own
  format and rely on the server or a client-side parser to
  translate.
- ⚠️ "Specially designed function call format" (observed on
  Qwen3-Coder's HuggingFace card) — might mean the model
  emits a format designed for a specific agent harness (Qwen
  Code, CLINE) that needs translation. If the hosting layer
  (NIM, OpenRouter, etc.) does the translation, fine. If
  not, FITT sees the raw format and breaks.
- ❌ No mention of function calling — assume it's not
  supported or works via prompted parsing only, both of
  which are unreliable.

The provider page for a hosted model (NIM, OpenRouter,
Together, etc.) also matters. A model that emits OpenAI-format
tool_calls natively but is served by a provider that doesn't
expose the `tools` parameter is effectively not function-
calling-capable for FITT. Check the provider's API reference.

### 2. Reliability under long context

Real FITT sessions accumulate history. Even with compaction
(when we have it), the model sees multi-turn tool-use
trajectories, and needs to emit clean tool_calls on turn 30
the way it did on turn 1. The "lost in the middle" effect is
real for every model, but the inflection point varies.

Signals:

- **BFCL multi-turn scores** (Berkeley Function Calling
  Leaderboard). The `multi_turn` category specifically tests
  format adherence over chained tool calls. Single-turn
  scores are less interesting — most models do OK on turn 1
  and diverge from there.
- **Reported context window size.** Treat spec-sheet numbers
  as upper bounds, not working ranges. The research
  consensus is that models start degrading noticeably
  around 50% of stated context. A model with 128K stated
  context gives you ~64K of usable working context for
  reliable tool-calling. A 256K model gives you ~128K.

### 3. Tool-calling discipline under FITT's actual system prompt

Distinct from criterion 2 (which is about *message list* growth
across turns). This one is about *system prompt* size on turn 1.

The model's tool-calling abstract benchmarks (BFCL, model card
tests) all use minimal system prompts. FITT injects ~1-5K
tokens of capability block + identity + skills + lessons before
the user's message ever arrives. Models advertised as "supports
tool calling" can pass a 200-token canary cleanly and lose
discipline at 5K — the post-training that teaches "emit
`tool_calls`" fights the post-training that teaches "follow
long instructions." The discipline that drops first is the
one we care about.

Observed: granite3.3:8b on 2026-05-22 emitted clean
`tool_calls` against Ollama directly (141 prompt tokens) and
narrated JSON inside `message.content` against the same model
through FITT (5400 prompt tokens). Same backend, same wire
format, only the system prompt size changed. Documented as the
incident entry in `docs/observed-issues.md`.

Inflection points by parameter count, observed in our use:

- **≤8B models**: discipline starts wobbling around 4-6K
  tokens of system prompt. Granite 3.3 8B narrates
  reliably above ~5K. Treat 8B-class as "fine for chat-only
  aliases or for tool turns when the system prompt is
  trimmed."
- **12-14B models** (qwen3:14b, mistral-nemo:12b): more
  resilient; we've seen them hold through 8-10K-token
  prompts.
- **30B+ dense or MoE-with-agentic-training**: hold reliably
  through FITT's full 5-10K-token prompts in our
  observations to date.
- **Cloud frontier** (Claude, GPT-4o family): no observed
  ceiling at FITT's prompt sizes.

How to check before binding:

1. **Bare check.** Direct curl against the backend with a
   minimal prompt and one tool. Confirms the model can
   tool-call at all. The boot probe (`alias_probe`) does this
   automatically.
2. **Realistic check.** The same alias hit through the FITT
   gateway with the user's actual workload (a typical
   Telegram-style "search the web for X" prompt). The diff
   between bare and realistic is the diagnostic. As of Phase
   7 (visibility & traceability) the per-turn capture makes
   this visible after the fact; an explicit
   `fitt eval alias <name> --realistic` flag is in the
   opportunistic backlog.
3. **Approximate the budget.** FITT's typical injected
   system prompt is 4-6K tokens with the default capability
   block, identity, and ~10 lessons. Add per-skill and
   per-MCP-tool overhead; subtract if you've trimmed.
   Compare to the model's observed inflection point.

What this rules out, in practice, for FITT's
agent-shaped aliases (`fitt-default`, `fitt-smart`):

- 8B models bound to aliases that flow through Telegram
  / cron / tool turns. Use them for chat-only aliases
  (`fitt-fast` for short-question paraphrasing) where
  tool-calling isn't required.
- Models advertised as "specially designed function call
  format" without explicit OpenAI-compatible coverage —
  cumulative with criterion 1's flag.

Mitigations short of a rebind:

- **Compact-prompt mode** (Phase 7+ opportunistic).
  `tools.compact_capability_block: true` skips the prose
  trailer in the capability block, rendering only the tool
  list. Buys back ~500-1500 tokens.
- **Trim identity / lessons.** Phase 5's lessons store
  caps at 50 entries; that's already a lot. If your
  identity files are over 1K tokens, ask whether all of it
  is load-bearing.
- **Per-alias system-prompt overrides** (not yet shipped).
  A future config option could let small-model aliases
  receive a slim variant of the prompt while
  large-model aliases get the full one. Worth
  considering when the realistic-eval data shows the
  decision is forced by binding, not preference.

### 4. Architecture signal: dense vs MoE

Dense models tend to be more consistent about structured
output format than MoE models with sparse activation. The
intuition: MoE sparsity helps reasoning capacity per compute,
but the "discipline" for strict output formatting is spread
across experts and doesn't always route cleanly.

This is a heuristic, not a law. Qwen3-Coder-480B-A35B is MoE
(35B active of 480B) and Qwen claims it's top-tier on agentic
tool-use — explicitly trained to overcome the general MoE
weakness in format adherence.

So: MoE models can be great at tool-calling, but only when
the post-training specifically targeted it. Check for this
in the model's blog or paper. Absent evidence, prefer dense.

Rule of thumb applied to FITT:

- Dense 70B-class model with tool-use post-training: safe
  default.
- MoE model explicitly trained for agentic tool use: likely
  good but test.
- MoE model trained primarily for general reasoning or
  coding *without* agentic-tool-use training: likely bad
  at FITT's workload, even if benchmarks look good.

`qwen3-next-80b-a3b-instruct` (the one that bit us) is the
third category. Qwen3-Coder is the second. The recommendation
trail in the hallucinations doc under-weighted this distinction.

### 5. Reasoning capacity

For `fitt-smart` specifically, the orchestrator role needs
enough reasoning to handle multi-step tasks: understanding
which tool to call, composing tool results, backtracking on
failure. 70B-class dense or 35B-active MoE is the minimum
practical range. Below that you get reliable tool-calling
but weak reasoning; above that you get diminishing returns
for FITT's current workload.

For `fitt-fast`, the trade is reversed: short conversational
tasks, weather lookups, quick confirmations. 8B-class models
are plenty if they tool-call cleanly.

For `fitt-default`, somewhere in between.

### 6. Availability and cost profile

Under FITT's no-subscription constraint:

- **NVIDIA NIM** — free tier exists, serves a growing
  catalog of open models. Current list at
  `build.nvidia.com/models`. Ask about rate limits per
  model; they vary.
- **OpenRouter** — some free-tier open-weight models with
  rate limits, plus paid pay-per-token access to many more.
  "Paid" here means per-token, not subscription — fits the
  constraint.
- **Together.ai, Groq, Fireworks** — similar shape to
  OpenRouter, pay-per-token access to open-weight models.
  Worth having as fallbacks if NIM has an outage.
- **Local Ollama** — for models small enough to run on the
  hardware we have (8B class is painless on a consumer GPU;
  32B needs attention; 70B+ needs serious VRAM). Tool-
  calling reliability varies wildly between Ollama's
  quantized models and the full-precision reference
  implementations; verify on your setup.

Rate limits matter more than cost for FITT's usage pattern
(one-person-use, small number of sessions). A "cheap" model
that rate-limits at 10 RPM is less useful than a slightly
more expensive one at 100 RPM.

### 7. Licensing

For the "shareable by construction" principle, check the
license. Most open-weight models (Llama, Qwen, DeepSeek,
Mistral Small, Gemma) are permissive enough for personal
use. Some (Mistral Large, certain research models) have
research-only or named-entity restrictions. Not a concern
for your private use but worth flagging if FITT's design
changes to involve others.

## How to apply these to a real decision

Concrete process when picking a new `fitt-smart` backend.

### Step 1: Build the candidate list

**Go to the live provider catalog, not a cached search
result.** The NIM / OpenRouter / Together / Groq catalogs
churn fast enough that a month-old snapshot is unreliable.
Web search can surface families and names to investigate —
it shouldn't be the source of truth for what's currently
available.

For NVIDIA NIM today:

1. Open `build.nvidia.com/models` in a browser. (Web-fetch
   gives you only a shell of the page; the catalog list is
   JS-rendered. Eyes on screen beats scraping here.)
2. Filter by the model families that are known to support
   tool calling well: DeepSeek, Qwen (coder variants),
   Llama (3.3+ or 4), Mistral.
3. Pull the model cards for the 3-5 that look most
   promising. Skim for: "function calling," "agentic
   tool use," "OpenAI-compatible," "native function
   calling," "specially designed format" (flag).
4. Note the context window and architecture (dense /
   MoE-active-params).
5. Cross-check against DeepSeek / Qwen / Meta's own release
   blogs to confirm what was trained for tool use vs. what
   just supports it nominally.

### Step 2: Cross-reference BFCL

- Go to `gorilla.cs.berkeley.edu/leaderboard`. The page is
  JS-rendered; be patient or load in a browser.
- Filter by "Function Calling" (FC) not "Prompt" — we only
  care about native tool_calls.
- Look up each candidate's scores, especially the
  `multi_turn` category.
- Prefer 70%+ on multi_turn. Below 60% means you'll be
  debugging failures as a regular part of your day.

### Step 3: Check the model's own claims with skepticism

The model's announcement blog (e.g. `qwenlm.github.io`,
`deepseek.com`) will tell you what the model was trained
for. Useful for confirming whether an MoE model's agentic
reliability is real (explicit agentic post-training) or
accidental (general reasoning only).

Red flag phrases:

- "Specially designed function call format" — may not be
  OpenAI-compatible, verify.
- "Optimized for [specific agent framework]" — same,
  verify it works outside that framework.
- "State-of-the-art" claims without a BFCL number —
  marketing; check benchmarks independently.

### Step 4: Test against the failing prompts

Before binding an alias, run the model against the prompts
that broke the previous one. For FITT's 2026-05-10 session,
that means:

- "Read the first 5 lines of README.md." (should emit a
  real `read_file` tool_call)
- "Is there a test file for http_get?" (should emit a
  `glob_search` tool_call)
- "What's the weather tomorrow in Medford, MA?" (should
  emit `http_get` and not fabricate content)

Three prompts, three fresh sessions, one model. If any of
the three produces narration instead of a tool_call, try
the next candidate.

### Step 5: Two-week live trial

Bind the alias, use FITT normally for two weeks (Principle 9),
and watch `fitt inbox` and `observed-issues.md` for new
failure modes. If it holds up, it stays. If not, loop back
to Step 1 with the new failure data.

## Worked example: the 2026-05 `fitt-smart` swap

This is how the process applied when we re-evaluated the
qwen3-next-80b-a3b-instruct choice.

### Why the original choice was made

Unclear in retrospect. Qwen3-Next was NVIDIA's newest Qwen
at the time; "newest" is a reasonable default when you
don't have a specific benchmark target. The choice wasn't
informed by BFCL scores, tool-call format verification, or
the dense-vs-MoE distinction — because we hadn't built
those criteria yet.

### What we learned from live use

- 2026-05-08: produced TOOL_NAME/BEGIN_ARG/END_ARG
  sentinel narration 5/5 times on a simple file-read
  prompt. Classic "prompted function calling" shape —
  model trained on tool-use but not on strict OpenAI
  format. Reasonable guess: Qwen's "specially designed
  function call format" heritage at play, with Qwen3-Next
  specifically weak because it's MoE-sparse-activation
  general-reasoning, not agentic-post-trained.
- 2026-05-10: 40%+ tool call failure rate across a
  21-hour session once context poisoning kicked in.

Verdict: the model is reasoning-capable but tool-call-
unreliable for FITT's workload. Replace.

### Candidates under consideration (as of 2026-05-11)

Applying Step 1 against NIM. The original draft of this
section listed `deepseek-v3.1-terminus` as a candidate; on
2026-05-11 you checked build.nvidia.com directly and it
wasn't there — DeepSeek-V4 (Flash and Pro) had replaced it.
Lesson learned: web-search snapshots of NIM's catalog go
stale fast; always confirm against the live catalog at
decision time. Corrected candidate list below:

| Model | Arch | Context | Function calling advertised | Risk |
|---|---|---|---|---|
| `deepseek-ai/deepseek-v4-flash` | MoE 284B/13B-active | 1M | "Native function calling (128 parallel calls)." Pre-tuned adapters for Claude Code, OpenCode, OpenClaw. Blog tagline: "a million-token context that agents can actually use." | Low. MIT license. Explicit agentic post-training (the MoE-agentic distinction from Criterion 4). Lighter than V4-Pro so better Telegram latency. |
| `deepseek-ai/deepseek-v4-pro` | MoE 1.6T/49B-active | 1M | Same V4 family; "Open-source SOTA in Agentic Coding benchmarks." | Low-to-medium. Same family win, but 1.6T total means slower and likely harder-rate-limited on NIM free tier. Save for heavy work if V4-Flash falls short. |
| `qwen/qwen3-coder-480b-a35b-instruct` | MoE 480B/35B-active | 256K | "Supports function calling and tool choice." "Specially designed function call format." | Medium. Claims look good but "specially designed format" is exactly the phrase that bit us last time with the Qwen family. |
| `meta/llama-3_3-70b-instruct` | Dense 70B | 128K | Long track record of OpenAI-compatible tool calls. | Low. Conservative dense default. Less sparkle on reasoning than the MoE candidates but more predictable format adherence. |

### Recommendation trail for this specific swap

1. **First try: `deepseek-v4-flash`.** Best match to FITT's
   needs: MIT license, explicit agentic-tool-use training,
   native function calling, 1M context, smaller enough to
   stay fast on Telegram. If the three test prompts work,
   bind and done.
2. **Second try: `qwen3-coder-480b-a35b-instruct`.** Strong
   agentic claims, but the "specially designed format"
   phrasing needs empirical verification given the Qwen-
   family lineage that just failed us. If prompts work,
   bind. If narration appears, skip.
3. **Third try: `deepseek-v4-pro`.** Same V4 agentic
   benefits but bigger; reach for this only if Flash
   struggles on harder reasoning tasks during the two-week
   trial.
4. **Fourth try: `llama-3.3-70b-instruct`.** Conservative
   fallback. Dense 70B with mature tool-calling, well-
   characterised behaviour.
5. **Stopping point:** one of the above works, or we escalate
   to "maybe the gateway code is the problem, not the model"
   (Problem B / `_persisted_args` bug per observed-issues).

### Why Qwen3-Coder was almost first but isn't

Initial instinct: it's agentic-trained, it's got the best
marketing around tool use among open Qwen variants, it
should be the default. But:

- Same family (Qwen) as the model that just failed. Not
  proof it'll also fail — Qwen3-Coder has explicit agentic
  post-training that qwen3-next lacks — but it's a prior
  worth taking seriously.
- "Specially designed function call format" on the HF page
  is specifically the phrase that describes what went wrong
  last time: a format that works inside Qwen's own agent
  harness but doesn't always emit clean OpenAI tool_calls
  through third-party wrappers.
- DeepSeek-V4 has the stronger vendor claim ("native function
  calling," MIT license) and a different lineage. If we're
  debugging a Qwen-specific problem, the cleanest test is a
  non-Qwen model first.

If V4-Flash fails, Qwen3-Coder is the immediate next try,
and we learn whether the "specially designed format" was the
issue or not.

### Lesson logged: verify the catalog, don't trust search snapshots

The original draft of this doc recommended
`deepseek-v3.1-terminus` as the first candidate. That model
appeared in web search results with a publication date from
days-to-weeks ago. When we checked `build.nvidia.com` on
2026-05-11, NIM had moved on: v3.1-terminus was gone,
replaced by V4-Flash and V4-Pro.

For the process described above, this means: **Step 1 must
be a live check against `build.nvidia.com/models`, not a
cached search result.** Same discipline applies to any
hosted-model catalog (OpenRouter, Together.ai, Groq), which
all churn fast enough that a month-old snapshot is
unreliable. Web search can surface candidates to investigate,
but the candidate list gets confirmed against the live page
before testing.

## Where the eval harness fits

The criteria above are the *reasoning*; the eval harness is the
*tooling* that makes applying them fast. It now exists (it didn't when
the first draft of this doc was written) — see the operational runbook
below. Given an alias it runs a curated set of prompts, records whether
`tool_calls` were emitted correctly, and writes a pass/fail report,
turning "run three prompts by hand" into `fitt eval alias <name>` and a
model swap into a minutes-long decision with evidence.

## Onboarding a new model — operational runbook

The concrete steps to evaluate and bind a candidate, using the tooling
that exists today. Worked end-to-end on `gemma4:12b-it-qat` 2026-06-15.
Layered cheapest-first: each step is a cheap gate before the more
expensive next one, so a bad candidate fails fast.

**The order matters.** A model that passes the bare suite can still
fail under FITT's real prompt (the granite trap, Criterion 3) — so the
realistic step is the actual decision-maker, not the bare one.

### 0. Make the model reachable

- **Local Ollama** (small enough for your GPU): `ollama pull <model>`,
  endpoint `http://localhost:11434`.
- **EC2 A10G** (bigger models / no local GPU): pull on the box and
  reach it over the SSH local-forward tunnel — see
  `~/.fitt/ec2-runbook.md`. `ssh ec2-instance-1 "ollama pull <model>"`
  (the pull rides the SSH connection; the `-L 11435:localhost:11434`
  tunnel is only needed for the *gateway* to reach it). Gotcha: the
  SSM session drops periodically ("Bad packet length") — restart the
  tunnel when it does.
- **Warm it first.** A cold 7–10GB model can blow the 10s boot-probe
  timeout on its first call. One throwaway `/api/generate` call loads
  it into VRAM so the probe/eval get clean timings.

### 1. Add the model + alias (config only — Principle 7)

In `~/.fitt/config.yaml`, add a `models:` entry and bind an alias.
No code change:

```yaml
aliases:
  fitt-ec2-gemma4: gemma4-12b-it-qat-ec2
models:
  - id: gemma4-12b-it-qat-ec2
    backend: ollama
    endpoint: http://localhost:11435   # EC2 via tunnel
    model: gemma4:12b-it-qat
```

Check `/api/tags` for the model's declared `capabilities` while you're
there — `["completion","tools","thinking","vision"]` tells you whether
it nominally supports tools and whether it's a **thinking model** (see
step 5).

### 2. Boot probe — the instant canary

Start the gateway; `alias_probe` fires one canary tool-call per alias
at boot and logs `alias_probe.ok` ("emitted N tool call(s) as
expected") or a failure. This is the cheapest "can it tool-call at
all" signal. A `narrated` / no-tool result here means stop — the
binding is unusable for agent-shaped aliases.

### 3. Bare tool-calling suite

```
uv run fitt eval alias <alias> --suite default --timeout 120
```

Five cases: tool-call basics + correct *no-tool* discrimination on
small talk. Writes a report to `~/.fitt/eval/<alias>-latest.md`.
Necessary but not sufficient — this is the minimal-prompt check
(Criterion 1).

### 4. Realistic suite — the degradation diagnostic (the decision-maker)

The same cases plus a live-fact case, run under FITT's **full live
system prompt** (capability block + identity + skills + lessons). This
is the granite-incident diagnostic (Criterion 3): a model that's clean
on the bare suite can lose tool-call discipline at 5K+ prompt tokens.

It needs the running gateway's assembled prompt, so it's HTTP-only
today (the CLI `--suite` exposes only `default|coding`):

```
# gateway running on :8421
curl -s -XPOST "http://localhost:8421/v1/eval/<alias>?suite=realistic" \
  -H "Authorization: Bearer <token>"
```

The response header notes the realistic prompt's approximate token
count. *Known friction:* realistic isn't in the CLI, and there's no
single "run every suite" command — candidate improvements (see below).

### 5. Orchestration-readiness — plan + follow-through

Only if the alias will be orchestrated (planner or executor). There's
no dedicated suite yet; run a real multi-step orchestrated turn (enable
`orchestration` for the alias, send a "search the web for X and
summarise" turn) and read the captured turn / cassette.

**Thinking models need extra care here.** A reasoning model
(qwen3:14b, and gemma4 is also `thinking`) plans inside
`reasoning_content`, returns empty `content` and no `todowrite`, and
the loop reads that as "done" — no plan lands. The planner
continue-nudge (`run_planner_pass`, task 14b) is built to catch this;
confirm it fires for the new model. `planner_iterations: 2` does *not*
fix it. See `observed-issues.md` "Thinking-model planner stalls."

### 6. Multi-sample, because inference is non-deterministic

A single `5/5` can be `4/5` next run (model connectivity, streaming,
cold-load, the reasoning channel). For any rate claim, run k samples
and report the pass rate — don't trust one shot.
`.scratch/run_planner_multisample.py` does this for plan-election; the
principle generalises to any of the suites above. Exclude transient
infra failures (timeouts on the SSM path) from capability rates.

### 7. Record a cassette for regression

With the gateway started under `FITT_RECORD_CASSETTE=<path>`, the
validated turns are captured to a replay cassette
(`POST /v1/internal/record-flush`). Commit useful ones as fixtures so
the behaviour is pinned without needing the live model (see
`gateway/src/gateway/record_replay.py`).

### 8. Two-week live trial (Principle 9)

Bind it, use FITT normally for two weeks, watch `fitt inbox` and
`observed-issues.md`. Holds up → it stays; new failure modes → loop
back with the data.

### Candidate improvements to this workflow (not yet built)

- `fitt eval alias <name> --suite realistic` (CLI parity; needs the
  CLI to assemble the live prompt without a running app).
- `fitt eval alias <name> --all` — one command, all suites, one report.
- A first-class **orchestration-readiness** suite (plan-election +
  follow-through), feeding the per-dimension capability profile
  (phase12 task 24). That profile — per-dimension grades that *inform*
  config rather than a single tier — is the intended "best way" this
  runbook is the manual stand-in for.



## Current recommendations (volatile; updated when we swap)

**`fitt-smart` (swapped 2026-05-11):** now
`deepseek-ai/deepseek-v4-flash` via NIM. Early use report in
the decisions log below.

**`fitt-default`:** [unchanged]

**`fitt-fast`:** [unchanged]

## Decisions log

What actually got bound, when, and why. Newest first. Paired
with the alias tool-call probe (shipped 2026-05-11) — every
binding change produces a probe result at the next gateway
boot, which is the shortest loop we have between "swap" and
"evidence the swap holds."

### 2026-05-11 — `fitt-smart` → `deepseek-ai/deepseek-v4-flash`

**Swapped from:** `qwen/qwen3-next-80b-a3b-instruct` (bound
2026-05-08 without a rubric; failed on sentinel narration
2026-05-08, poisoned a 21-hour Telegram session 2026-05-10).

**Chose:** deepseek-v4-flash per the "first try" on the
recommendation trail above. MIT license, explicit agentic
post-training, native OpenAI-compatible function calling
(128 parallel calls claimed), 1M context window, MoE with
13B active — small enough to stay fast on Telegram.

**Applied rubric:**

- Criterion 1 (native tool_calls): vendor claim "native
  function calling," no "specially designed format" red
  flag. Pass.
- Criterion 2 (long-context reliability): 1M stated
  context, ~500K usable per the 50% rule. Plenty for
  FITT's sessions.
- Criterion 3 (system-prompt discipline): not directly
  evaluated at swap time; criterion was added 2026-05-22
  after the granite incident showed it matters as a
  separate axis. V4-Flash's 13B active params would put
  it in the "wobbly under 5K-token system prompts"
  bucket per the inflection-point heuristic, so the
  cloud-routing posture (`fitt-smart` is OpenRouter,
  not local) is what makes the binding safe — the
  effective behaviour is more like the cloud-frontier
  bucket than the 8B-class one. Revisit if a future
  binding moves V4-Flash on-prem.
- Criterion 4 (MoE + agentic post-training): explicit. Not
  the Qwen3-Next "MoE general reasoning without agentic
  training" anti-pattern.
- Criterion 5 (reasoning): 13B active is below the 35B
  rule-of-thumb minimum, but V4-Flash's agentic post-
  training compensates on the agentic-specific workload
  that matters for `fitt-smart`. Revisit if reasoning
  feels thin during the two-week trial.
- Criterion 6 (cost): free tier on NIM, same posture as the
  previous binding.
- Criterion 7 (license): MIT. Cleaner than Qwen's weight
  license.

**Verification path:**

1. Boot-time alias tool-call probe (`alias_probe.ok` log
   line on gateway start, single-canary signal).
2. Early use in Telegram and router-mode opencode. User
   report on 2026-05-11: "it's working ok."
3. Two-week live trial per Principle 9 ends 2026-05-25;
   check `observed-issues.md` and `fitt inbox` then.

**What to watch for before declaring the swap durable:**

- Sentinel / JSON-fence narration detected by the boot-time
  :mod:`gateway.alias_probe` and the on-demand
  `fitt eval alias <name>` harness. If either reports the
  alias as `narrated`, reconsider the binding. (We no longer
  fire a runtime `tool_call_narrated` event — live chat has
  no cheap way to know the user's intent, and the 2026-05-12
  rollback is documented in `observed-issues.md`.)
- Reasoning quality on multi-step tasks relative to the
  13B-active number. If V4-Flash stumbles on composing
  tool results, V4-Pro is the next step up (same family,
  49B active, slower).
- NIM rate limits on the free tier. Not yet observed with
  the previous binding; worth watching if Telegram latency
  drifts.

**If it degrades:** the recommendation trail's step 2 is
`qwen3-coder-480b-a35b-instruct`, step 3 is
`deepseek-v4-pro`, step 4 is `llama-3.3-70b-instruct`. Apply
the rubric fresh — the answer may have changed by the time
we re-run it.

## When to revisit

- A new major open-weight model ships (roughly monthly
  these days). Open the candidate list and see if anything
  obsoletes the current binding.
- `observed-issues.md` accumulates three or more
  tool-calling failures that can't be explained by the
  gateway or prompting.
- An eval harness run shows your current `fitt-smart`
  below 70% pass rate on the curated prompt set.
- You realise you've been avoiding using FITT for tasks
  you'd expect it to handle. Usually a signal the backend
  can't keep up.
