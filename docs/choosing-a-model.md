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

### 3. Architecture signal: dense vs MoE

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

### 4. Reasoning capacity

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

### 5. Availability and cost profile

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

### 6. Licensing

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

For NVIDIA NIM today (2026-05):

1. Go to `build.nvidia.com/models`.
2. Filter by the model families that are known to support
   tool calling well: DeepSeek, Qwen (coder variants),
   Llama (3.3+ or 4), Mistral.
3. Pull the model cards for the 3-5 that look most
   promising. Skim for: "function calling," "agentic
   tool use," "OpenAI-compatible," "specially designed
   format" (flag).
4. Note the context window and architecture (dense /
   MoE-active-params).

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

Applying Step 1 against NIM:

| Model | Arch | Context | Function calling advertised | BFCL | Risk |
|---|---|---|---|---|---|
| `deepseek-ai/deepseek-v3.1-terminus` | MoE 685B, sparse | 128K | "Strict function calling" explicitly. | Not looked up. | Low. Vendor-signed strict claim; DeepSeek has a track record of tool-use quality. |
| `qwen/qwen3-coder-480b-a35b-instruct` | MoE 480B/35B-active | 256K | "Supports function calling and tool choice." "Specially designed function call format." | Qwen claims Claude Sonnet 4-comparable on agentic. | Medium. Claims look good but "specially designed format" is exactly the phrase that bit us last time. |
| `meta/llama-4-maverick-17b-128e` | MoE 17B/128E | varies | NIM VLM docs list function-calling-supported models incl. Llama 4 Maverick. | Not looked up. | Medium. Meta does tool-use post-training, but Llama 4 is recent and less proven than Llama 3.3. |
| `meta/llama-3_3-70b-instruct` | Dense 70B | 128K | Long track record of OpenAI-compatible tool calls. | Solid on BFCL historically. | Low. The conservative dense default. |

### Recommendation trail for this specific swap

1. **First try: `deepseek-v3.1-terminus`.** Vendor-signed
   "strict function calling" claim makes it the lowest-risk
   shot at solving the problem outright. If the three test
   prompts work, bind and done.
2. **Second try: `qwen3-coder-480b-a35b-instruct`.** Strong
   agentic claims, but the "specially designed format" phrase
   needs empirical verification. If test prompts work, bind.
   If narration appears, it's the same class of failure as
   qwen3-next and we skip.
3. **Third try: `llama-3.3-70b-instruct`.** Conservative
   fallback. Dense 70B with mature tool-calling, well-
   characterised behaviour. Less sparkle on reasoning than
   the two above but more predictable.
4. **Stopping point:** one of the above works, or we escalate
   to "maybe the gateway code is the problem, not the model"
   (Problem B / `_persisted_args` bug per observed-issues).

### Why Qwen3-Coder was almost first but isn't

Initial instinct: it's agentic-trained, it's got the best
marketing around tool use among open models, it should be
the default. But:

- Same family (Qwen) as the model that just failed. Not
  proof it'll also fail — Qwen3-Coder has explicit agentic
  post-training that qwen3-next lacks — but it's a prior
  worth taking seriously.
- "Specially designed function call format" on the HF page
  is specifically the phrase that describes what went wrong
  last time: a format that works inside Qwen's own agent
  harness but doesn't always emit clean OpenAI tool_calls
  through third-party wrappers.
- DeepSeek-V3.1-Terminus has the stronger vendor claim
  ("strict") and a different lineage. If we're debugging a
  Qwen-specific problem, the cleanest test is a non-Qwen
  model first.

If DeepSeek fails, Qwen3-Coder is the immediate next try,
and we learn whether the "specially designed format" was the
issue or not.

## Where the eval harness fits

The process above is manual and ad-hoc. The hallucinations
doc proposes building an eval harness (item 6 of the action
list) that automates Steps 2–4: given an alias, run a curated
set of prompts, record whether tool_calls were emitted
correctly, produce a pass/fail report. That harness would:

- Replace "run three prompts by hand" with
  `fitt eval alias fitt-smart`.
- Make model swaps a one-minute decision with evidence,
  not a half-day investigation.
- Catch regressions when a provider changes a model's
  underlying weights or serving stack.

Until that exists, this doc is the process.

## Current recommendations (volatile; updated when we swap)

**`fitt-smart` (2026-05-11):** needs swapping. Currently
`qwen/qwen3-next-80b-a3b-instruct`. Candidate trail above.

**`fitt-default`:** [unchanged]

**`fitt-fast`:** [unchanged]

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
