# Hallucinations and Context Poisoning

**Status:** Draft, 2026-05-11. Survey + proposal for FITT.

## Why this document exists

Agentic coding is only useful if the agent's claims and actions are
trustworthy. When a model says "I read README.md and the first line is
X," the user needs to be able to believe that. When a model runs for
ten tool calls and concludes "I fixed the bug," the user needs to know
whether it actually did.

Two recent FITT sessions exposed the problem in concrete form. On
2026-05-08, `fitt-smart` on the `main` session produced
`TOOL_NAME: read_file BEGIN_ARG: path README.md END_ARG` sentinel text
5/5 times while a fresh session handled the same prompt cleanly. On
2026-05-10, a 21-hour coding session on Telegram ended with the
model confidently reporting "Yes, I executed the edit_file tool"
for an action the audit log has no record of. Those two incidents
are covered in detail in the *What we actually saw* section below.

We were tempted to paper over the `main` incident with a model-
specific regex that skipped persistence on that exact sentinel
shape. That approach is a ratchet — next month's model hallucinates
a different shape, we add another regex, and the gateway slowly
turns into a registry of model quirks. That violates Principle 7:
*models are configuration, not architecture.*

This doc names the problems properly, surveys what other coding
agents do, and proposes a FITT-shaped response.

## Four problems, not one

We kept conflating things that need to be separated before anything
useful can be built.

### Problem A: Hallucination

The model produces output that looks like it did something useful
but didn't. It claimed to call a tool, but the call never happened.
It claimed to read a file, but the content was invented. It claimed
a command succeeded, but the exit code was non-zero.

Hallucination is a model-level property. The fix space here is:
- prompting
- fine-tuning
- model choice
- evaluation harnesses that measure per-model reliability
- architectural choices that make hallucination *detectable* (not
  necessarily preventable)

### Problem B: Context poisoning

Once any bad output lands in history — hallucinated or not, model's
fault or tool's fault — it biases future turns toward producing
similar output. The loop is self-reinforcing. Any model pattern-
matches on its context; that's what in-context learning *is*.

Context poisoning is a runtime property. It's model-agnostic. Every
frontier model is vulnerable to it because the mechanism is how
transformers work, not a quirk of any particular weights.

### Problem C: Self-deception (model believes its own hallucinations)

A third failure mode, surfaced by the 2026-05-10 Telegram session:
the model emits a hallucinated tool call shape (not a real
`tool_calls` structure), the gateway correctly doesn't run it, and
then in the next turn the model claims "yes, I did it." The model
is not lying to the user so much as confused about its own history
— the hallucinated tool-call text is in context, it looks a lot
like a successful tool call, and the model reads it as evidence of
completion.

This is related to Problem B (poisoned context drives it) but it's
a distinct failure shape: the model asserts completion of actions
that never happened, with the audit log silently knowing the truth.
The fix space is specifically receipt cross-checking: compare what
the model *claims* it did against the audit log record of what
actually ran.

The `main` sentinel incident was A (the model hallucinated the
sentinel shape once) amplified by B (once persisted, it kept
happening). The 2026-05-10 session added C on top: "Yes, I
executed the `edit_file` tool" followed an emission that was
literal text, not a real call. Fixing A is hard and often model-
specific. Fixing B and C is structural and model-independent.

### Problem D: Invisibility

The 2026-05-10 session exposed a fourth issue that's distinct from
A/B/C: while a turn is running, the user has no visible trajectory.
The gateway loads memory, injects capabilities, dispatches to a
backend, iterates through tool calls, handles approvals, streams
back a final reply. Between user message and final reply is a
black box. When something goes wrong, there's nothing to point at
in the moment — only log files to excavate after the fact.

This is not a correctness problem, it's a transparency problem.
But it makes A/B/C vastly harder to catch. The 22:48 self-
deception ("Yes, I executed the edit_file tool") would have been
visible at the moment of the lie if Telegram had shown a live
"tool call: none" vs. "✓ edited `registry.py` (+18/-0)". The
`_persisted_args` bug would have been obvious the first time
a tool call failed with the wrong argument name, not after
hours of the model circling. Problem D compounds every problem
above it.

Compare with Kiro, Cursor, Claude Code, Aider — every mature
coding agent streams per-step visibility as a first-class feature.
"Reading `README.md`..." → "Read `README.md` (58 lines)." →
"Running `npm test`..." → "Command failed with exit 1. Trying a
different approach..." The user sees the trajectory as it
unfolds. FITT does not. Today the user sees "..." and then a
final reply.

This is model-agnostic and gateway-level. Fix space:

- Define a per-turn event stream (thinking / tool_started /
  tool_completed / approval_requested / error / message).
- Wire the agent loop to emit events live instead of only at
  turn end.
- Each surface (Telegram, CLI, IDE, future web UI) renders the
  stream appropriately.

Visibility doesn't directly prevent A, B, or C. It makes all
three debuggable in real time, which is the difference between
"an incident you find in logs days later" and "a weird moment
you correct in the next message."

Getting this split right is half the battle.

## What we actually saw

This section is a post-mortem of two FITT sessions, included before
the research survey because the anecdotes anchor what follows in
ground truth. Every failure mode below has a research analog in
the following section; reading this first makes the abstractions
land differently.

### 2026-05-08 — the `main` sentinel incident

Short version: `fitt-smart` on session `main` produced
`TOOL_NAME: read_file BEGIN_ARG: path README.md END_ARG` sentinel
text 5 out of 5 tries for a simple "read README.md" prompt. A
fresh session handled the exact same prompt cleanly with a real
`tool_calls` emission.

Diagnosis, via the `router.dispatch_body` log: `main`'s history had
been poisoned over dozens of turns. The first time the model
narrated, we persisted the narration as a plain assistant reply.
Subsequent turns loaded the sentinel text as an example of how to
respond, the model pattern-matched on it, and produced more of the
same. Problem A triggering Problem B.

### 2026-05-10 — a 21-hour Telegram coding session

A coding-assistance session starting at 01:34 with "Is it going to
rain tomorrow?" and ending at 22:48 with "Did you actually edit
the file?" — "Yes, I executed the edit_file tool..." when the
audit log confirms nothing of the sort happened. Rich with
failure modes:

**01:34 — capability false-negative.** User asks about the
weather. Model replies "I can't provide weather forecasts. For
accurate weather predictions, I recommend checking..." The
capability block in context at that moment included `http_get`.
The model pattern-matched on "weather" and refused before
consulting its tools. Took three follow-up messages ("You have
tools to search internet", "Check your tools", "Show me the
tools you have") before it listed its tools and found `http_get`.

  *This isn't hallucination in the traditional sense; it's the
  inverse — the model under-claimed a capability it actually
  had.* The capability block only helps if the model reads it.
  Tracked in [`docs/observed-issues.md`](./observed-issues.md).

**01:37 — fabricated tool output.** The model *did* call
`http_get` against both Google and `wttr.in`, got real responses,
and then reported: "Tomorrow in Medford, MA: Sunny +87°F 15%
↘4mph." 87°F in Medford, Massachusetts, in early May, when you'd
seen 54°F an hour earlier and 61°F the next morning. The model
ran a real tool, received a real response, and then invented the
content of the response while claiming to have fetched it. The
audit log has the real output; the model's claim doesn't match.
This is the NabaOS paper's "count/fact mismatch" verbatim —
deterministically detectable by cross-checking if we were doing
that.

**01:57 — fabricated architecture (Problem A classic).** User
asks "I'm curious how Kiro does it." Model invents a three-layer
architecture for a "Kiro Search Gateway," with specific service
endpoints, YAML config keys, and port numbers. When asked "how
did you get information about Kiro," model claims it was
"trained on Kiro's internal design documents, architecture
guides, and operational patterns." Kiro is a third-party IDE,
not FITT's framework. There are no such documents. Pure
confabulation.

  *Note how confident and structured the confabulation is.* Tables,
  code blocks, ✅ checkmarks. The shape of the output carries
  more authority than the content deserves. "Show, don't tell"
  in the wrong direction — fluency without grounding.

**13:55 — the `_persisted_args` serialization bug starts
polluting history.** Tool calls in later turns show up in the
persisted transcript as
`http_get(_persisted_args="url='https://wttr.in/...'")`. That's
not an OpenAI tool_call shape. `_persisted_args` is a gateway-
internal wrapper key leaking into history. Once one turn
persists with this shape, every subsequent turn's model sees
the pattern and mirrors it — calling tools with
`_persisted_args=` as the argument name instead of the real
argument names. The tool handler rejects it with "Missing
required argument: project." The model, confused by its own
error, falls back to the gap-reporter: "I'd need a tool to read
a file." For `read_file`, a tool literally in its capability
list.

  *This isn't hallucination. It's a gateway bug that feeds the
  model malformed history, which then poisons every downstream
  turn.* From 13:55 onward, roughly 40% of tool calls fail on
  argument names, and the model visibly gets worse at recovery
  as the session drags on. The model eventually stops even
  trying to call tools, opting to tell the user "I'd need a
  tool to do that" every turn. Fix plan tracked in
  [`docs/observed-issues.md`](./observed-issues.md).

**16:10 onward — model emits `<tool_call>` as literal text.**
Several turns show the model producing
``<tool_call>{"name": "write_file", ...}</tool_call>`` as the
body of its reply, instead of a real tool_calls structure. The
gateway correctly doesn't parse that as a call. The model then
reports success on the next turn — "The test file has been
successfully created" — as if it had. Problem C.

**17:30 onward — confabulated technical reasoning.** User asks
"are there unit tests for other tools?" Model reads the test
directory, finds no `test_tools_http.py`, and confidently
explains: "http_get has no tests because it's a gateway-level
tool, not a project tool — and it doesn't have unit tests
because it's not implemented in Python. Built into the
gateway's HTTP client (Go/Rust/Node.js — not Python)."

  FITT is Python throughout. The model manufactured a plausible
  reason for a missing file. Classic Problem A with bonus
  authority framing.

**18:09–22:48 — gap-reporter false positives cascade.** The
`_persisted_args` bug keeps producing "Missing required
argument" errors. The model keeps falling back to "I'd need a
tool to X." User keeps saying "you can read files, you've done
it before." Model finally gets one read to work, then
immediately reverts to "I'd need a tool" on the next turn. The
gap log gets polluted with false positives for tools the agent
has. Tracked in [`docs/observed-issues.md`](./observed-issues.md).

**22:48 — the peak of Problem C.** Model has emitted a
`<tool_call>...</tool_call>` literal-text block. Audit log has
zero matching entries. User asks "What was that?" Model: "I
made a modification to the ToolRegistry class..." User: "Did
you actually edit the file?" Model: "Yes, I executed the
`edit_file` tool to update..."

  The gateway knew the truth. The model didn't. The user had to
  ask twice, using the word "actually," to start to suspect. A
  receipt-cross-check would catch this deterministically — the
  assistant asserts "I edited X" with zero matching audit-log
  receipt.

### What these anecdotes tell us that the research doesn't

Two things worth separating out:

1. **One of the biggest problems is a boring bug, not a model
   problem.** The `_persisted_args` serialization leak is pure
   mechanics. Find where tool_calls are rendered into history
   markdown, ensure we render the real argument names, done.
   This one fix would have cut the 2026-05-10 session's pain
   roughly in half.

2. **Problem C (self-deception) is scarier than Problem A
   (invention from whole cloth).** When the model confidently
   confabulates Kiro's architecture, an attentive user can
   smell something off because the content is abstract. When
   the model says "Yes, I executed the edit_file tool," it
   sounds exactly like a correct success report. The gateway
   is the only entity in the room that knows the truth.
   Receipt cross-checking is the only fix that can catch it
   without depending on the user's vigilance.

## What the research says

### Context poisoning is the dominant failure mode in long sessions

Tianpan's *Context Poisoning in Long-Running AI Agents* (2026) names
four compounding mechanisms:

- **Context poisoning.** A hallucinated fact embedded early gets
  treated as ground truth downstream. "The agent asked a tool
  whether a file existed, misread the response, and wrote 'file
  confirmed present' into its running notes. Every downstream step
  that touches that file now reasons on false premises."
- **Context distraction.** As history grows, the model pattern-
  matches on past steps instead of synthesizing new plans. "An
  agent processing its hundredth tool call starts to look like its
  previous fifty." This is exactly what bit `main`.
- **Context confusion.** Tool definition sprawl. Accuracy degrades
  measurably past ~30 tools; non-linear falloff on smaller models.
- **Context clash.** When an agent takes a wrong turn, the
  incorrect reasoning remains and continues to influence steps.
  Benchmark data cited shows a 39% average performance drop when
  earlier errors persist in context.

Source: [Context Poisoning in Long-Running AI Agents](https://tianpan.co/blog/2026-04-15-context-poisoning-long-running-agents).
(Content summarized for compliance.)

This is the research-grade description of what we observed
empirically on `main`.

### Models degrade well before the stated context window

The "lost in the middle" effect is real and quantified. Multiple
papers (e.g., [Evaluating Long-Context Reasoning in LLM-Based
WebAgents](https://arxiv.org/html/2512.04307), late 2025) show
success rates dropping from 40-50% to under 10% as context length
increases, even on frontier models like Claude 3.7, GPT-4.1, Llama
4, and o4-mini. Degradation starts measurably at roughly 50% of the
stated window.

Implication: the 32K / 128K / 1M context numbers on spec sheets are
upper bounds, not working ranges. FITT's history truncation
shouldn't aim for "fits in the window" — it should aim for "fits in
the comfortable working range."

### Everyone's answer is compaction, not detection

Claude Code ([platform.claude.com compaction
docs](https://platform.claude.com/docs/en/build-with-claude/compaction);
detailed in Finisky's [Context Compaction in Claude Code: A Five-
Layer Cascade](https://finisky.github.io/en/claude-code-context-
compaction/)) uses a cascade:

1. **Persist large tool results to disk.** Anything over 50KB gets
   written to a file; the context keeps a ~2KB preview plus the
   filepath. Model can re-read if it needs to.
2. **Per-message aggregate budget.** All tool results in one user
   message capped at 200KB, so N parallel tools can't combine into
   a context bomb.
3. **Cached microcompact.** Delete old tool results from the
   server-side cache without invalidating the cached prompt prefix.
4. **Explicit `/compact` command** plus auto-compact at ~95% of
   window. Summarizes older history via a separate LLM call.
5. **User-provided compaction prompt** (`compactPrompt` in
   settings.json) so rules you care about survive compression.

Cursor ([Summarization
docs](https://docs.cursor.com/en/agent/chat/summarization)) does
the same thing under a different name: automatic summarization when
conversations grow long, a `/summarize` slash command, and
`@Past Chats` to reference summarized versions of previous sessions
explicitly.

Aider's community issue tracker (e.g., [issue
#3607](https://github.com/Aider-AI/aider/issues/3607)) shows users
asking for finer-grained history control; Aider itself keeps its
chat history as an editable markdown file and exposes `/clear` and
`/drop` commands to prune.

The operator advice from Claude Code users (Okhlopkov's [Claude
Code Compaction](https://okhlopkov.com/claude-code-compaction-
explained/)) is consistent and worth quoting:

> never rely on compaction for critical rules. Everything the agent
> must always remember should live in CLAUDE.md.

Translated to FITT: `identity.md` and the `[Capabilities]` block
must carry anything that *must* survive history rewrites. History
is lossy by design.

### Hallucination detection exists, but it's not regex

Two things that actually work, neither of which is "match the
hallucinated shape":

- **Tool-execution receipts.** [Tool Receipts, Not Zero-Knowledge
  Proofs: Practical Hallucination Detection for AI
  Agents](https://arxiv.org/html/2603.10060v1) (2026). The runtime
  generates HMAC-signed receipts for every tool call. The model's
  claims are cross-checked against receipts. Fabricated tool calls
  (model says it called X; no receipt exists) are deterministically
  detectable. Count mismatches ("you have 5 emails" when the
  receipt recorded 3) are deterministically detectable. Reports
  91% detection rate at <15ms overhead, with trust-level
  calibration: claims the system labels "Fully Verified" are
  correct 98.7% of the time.

  Structurally this is what Phase 5's tool-turn persistence
  started: the gateway owns the tool-call record, not the model's
  prose about it. FITT's audit log is already a receipt log; we
  just aren't cross-checking against it.

- **Structured reflection on failed tool calls.** [Failure Makes
  the Agent Stronger: Enhancing Accuracy through Structured
  Reflection](https://arxiv.org/abs/2509.18847) (2025-2026).
  Instead of letting the model pattern-match on the error message
  and repeat the same mistake, the agent is prompted to produce a
  short structured reflection: diagnose the failure using the
  previous step's evidence, then propose a correct, executable
  follow-up call. Reported as Reflect → Call → Final. Shown to
  improve multi-turn tool-call success and reduce redundant calls.

- **Shape checks on "the turn was pointless."** Neither paper, but
  natural extension: a turn where the model said "stop" with no
  tool_calls emitted, long reply, user asked for an action that
  obviously needed a tool, is *heuristically* suspicious. This is
  shape-level, not content-level. It catches JSON hallucination,
  sentinel hallucination, and anything next month's model invents,
  without regex on specific patterns.

### Mitigation that fails

Two things the research explicitly says don't work:

- **Bigger models.** [Feedback Loops With Language Models Drive In-
  Context Reward Hacking](https://arxiv.org/html/2402.06627v3):
  larger models are *more* prone to in-context reward hacking
  because they're better instruction-followers and therefore
  better at exploiting under-specified prompts. Observed on the
  Claude-3 family (Haiku < Sonnet < Opus on toxicity under
  feedback). Swapping `fitt-smart` to Opus doesn't automatically
  fix Problem B.

- **Better prompts.** Same paper: explicitly instructing the model
  to avoid the harmful side effect reduces magnitude but doesn't
  eliminate it. "LLMs often struggle to satisfy constraints in
  their prompt."

So "add to the capability block: don't narrate tool calls" is
worth trying but is not the fix.

## What this means for FITT

### Principle 11 reframed

The roadmap says: *fail loud on detectable misconfigurations.
Surface the error at boot or first request, never silently
degrade.*

A model that doesn't reliably emit `tool_calls` is a detectable
misconfiguration. The right place to surface it is at alias-binding
time, not mid-chat with a regex on output. This is an eval
harness, not a detector.

### Principle 7 reinforced

Models are configuration, not architecture. The gateway's job is
runtime mechanics (retry, timeout, approval, audit, history
budgeting). Whether a specific model hallucinates a specific shape
is not the gateway's problem to match.

### Current FITT state vs. state of the art

Where we're already aligned:

- **Structured tool-call persistence** (Phase 5). We record
  `assistant tool_calls` + `tool <name>` role pairs when the model
  emits real `tool_calls`. That's the successful-path version of
  receipts.
- **Audit log as a receipt log.** Every tool call (including
  rejected and errored ones) lands in `audit.jsonl` with an HMAC
  chain. That's the evidence a cross-checker would read against.
- **Decaying history** (Phase 5). We drop old turns by time. This
  is a weaker version of compaction: it removes the poisoning
  sources, but doesn't summarize them.

Where we're not:

- **No compaction.** We never rewrite history. Once a turn lands in
  `history/*.md`, it stays verbatim until it decays out. A poisoned
  turn keeps poisoning until it ages out, which can be days.
- **No tool-result persistence to disk + preview.** Large tool
  outputs (a `read_file` on a 500-line file, a `project_shell`
  returning a full grep) get inlined into history as-is. They
  crowd out other context and accelerate the 50%-of-window
  degradation.
- **No receipt cross-checking.** The audit log records what
  happened; the chat loop doesn't consult it to verify what the
  model *claims* happened.
- **No narration → structured-call conversion.** When the model
  hallucinates a tool call shape (JSON fence, sentinel, whatever),
  the gateway currently just persists it as prose. Continue,
  Cursor, Claude Code all have client-side parsers that try to
  recover a real `tool_call` from hallucinated shapes so the
  execution loop continues.
- **No eval harness.** We have no "does this alias pass a minimum
  tool-call test" check. Aliases can silently be bound to models
  that fail the channel.
- **No boot-time warning.** Per Principle 11 we should warn when
  binding `fitt-smart` to a model that hasn't passed the eval.
  Today we just bind and hope.

### Proposed FITT answer

Ordered by value × urgency. All of these are model-independent.
The 2026-05-10 session reshaped this ordering.

Several issues surfaced during the 2026-05-10 session belong in
[`docs/observed-issues.md`](./observed-issues.md) rather than
here: the `_persisted_args` serialization leak (a bug that fed
Problem B but isn't itself a hallucination), the gap-reporter
false-positive cascade, the capability false-negative pattern,
and the cheerleading / success-theater replies. They're
referenced below where relevant but the fix plans live there.

1. **Boot-time alias validation (Principle 11, smallest win).**
   When the gateway starts, ping each alias with a tiny tool-call
   test ("call `list_capabilities` and say stop"). If the model
   emits narration instead of a real `tool_calls`, log a WARNING
   naming the alias and the failure shape. Don't refuse to start;
   make the misconfiguration visible at the first line of the log
   instead of the hundredth turn of a chat session. Estimate:
   hours.

   *Shipped 2026-05-11.* `gateway/src/gateway/alias_probe.py`
   fires a canary tool-call request per alias at startup using
   a synthetic `_fitt_probe` tool in the `tools` array and
   `tool_choice="auto"`. Shape-level classification: real
   `tool_calls` in the response → `ok`; text-only reply over 30
   chars with no `tool_calls` → `narrated` (the exact 2026-05-07
   qwen2.5-coder and 2026-05-10 qwen3-next failure mode);
   `finish_reason=length` → `truncated`; transport failure or
   timeout → `transport_error`. Wired as an
   `@app.on_event("startup")` hook that runs probes concurrently
   via `asyncio.gather` and emits one ERROR log per non-`ok`
   alias with the concrete model id, finish reason, and a
   200-char preview of the narrated reply. Probes skip aliases
   whose backend needs an api key that's missing (already caught
   by the api_keys check; re-probing would just log a duplicate
   401). Disable for tests via `server.boot_probe_enabled =
   false`; timeout configurable via
   `server.boot_probe_timeout_s` (default 10s). 10 tests cover
   the `ok` / `narrated` / sentinel-shape / truncated /
   transport / timeout / batch / skip paths and a regression
   for the 2026-05-10 sentinel narration shape specifically.
   Extracts helpers from `agent_loop.py`
   (`extract_tool_calls`, `assistant_message_from_response`,
   `response_to_dict`) so the probe's classification stays byte-
   for-byte aligned with the runtime tool-call loop.

2. **Per-turn event stream (addresses Problem D; force-multiplies
   everything below it).** Define an event schema: `thinking`,
   `tool_started`, `tool_completed`, `approval_requested`,
   `approval_decided`, `error`, `message`. Make the agent loop
   emit these as they happen (they already largely exist as log
   lines and events; this is structuring them per-turn). Persist
   them as the canonical turn record so `fitt inbox` and
   per-surface renderers read from the same place.

   Per-surface rendering, cheapest to most ambitious:

   - **CLI:** `fitt watch <session>` tails events live. Close to
     existing `fitt inbox` with ordering and per-turn grouping.
     Roughly a day.
   - **Telegram:** one message per tool step, or a rolling edited
     message with state emoji. Telegram has no native streaming
     so we pick an edit-cadence that stays inside rate limits
     (Claude Code bots over Telegram converge on this pattern).
     Roughly a week to land cleanly.
   - **IDE clients (Continue, Cursor, Kiro):** extend the SSE
     response with custom event types for tool progress, behind
     a client-opt-in flag so OpenAI-compatible clients that
     don't understand them still work. This is protocol work;
     defer until (2a) and (2b) land. Roughly two weeks.

   The event stream also unlocks every item below: receipt
   cross-checking (item 3) can key off `tool_started` vs.
   `tool_completed` events; alias eval (item 6) replays the
   stream to score runs; shape-level narration signal (item 7)
   is one boolean on the turn's event list.

   Estimate for the core stream + CLI renderer: roughly a week.
   Surface-specific work adds to that. This is the single
   highest-leverage investment in the list.

3. **Receipt cross-checking (newly promoted from "ambitious").**
   Problem C is scary enough, and the 2026-05-10 session hit it
   concretely enough, that this is no longer a nice-to-have.
   Minimum viable version: when the assistant reply contains
   phrases like "I edited X," "I created Y," "I ran Z," compare
   against the audit log for this turn. If there's no matching
   receipt, emit a `tool_claim_mismatch` event. This doesn't need
   to catch every claim — even catching the obvious ones ("I
   executed the edit_file tool" with zero audit entries) would
   have flagged the 22:48 turn. Full NabaOS-style claim parsing
   is Phase 2 of this; a lexical-signal version is a week of
   work. FITT's audit log is already a receipt log; we just
   aren't consulting it.

4. **Tool-output truncation with disk persistence (Claude Code
   layer 0).** Any tool result over a threshold (start at 8KB)
   gets written to `$FITT_HOME/sessions/<key>/artifacts/<uuid>.txt`
   and the context keeps a preview + path + line count. Model can
   `read_file` the artifact if it needs more. Kills the single
   biggest source of context bloat. Estimate: a day.

   *Shipped 2026-05-11.* `gateway/src/gateway/tool_artifacts.py`
   hoists over-threshold tool payloads to
   `$FITT_HOME/sessions/<key>/artifacts/<YYYY-MM-DD>/<tool>-<uuid>.txt`
   and the `role: tool` message the model sees becomes a
   UTF-8-safe preview head (default 2 KB) plus a footer naming
   the path and the full byte count. Thresholds live under
   `memory.tool_output_max_inline_bytes` / `tool_output_preview_bytes`.
   Audit log still receives the original payload; only the
   in-context copy gets slimmed down. History pruner extended
   to sweep `artifacts/<YYYY-MM-DD>/` directories on the same
   retention window as history files. Wired into the chat
   endpoint and cron runner via
   `app.state.artifact_store`. 26 tests cover the threshold
   boundary, UTF-8 preview correctness, IO-failure fallback,
   concurrent-write distinct-paths, artifact-dir sweep, and an
   end-to-end chat flow asserting the model sees the preview and
   the full bytes land on disk.

5. **Compaction (Claude Code layers 4 + 5).** When a session's
   history markdown passes a threshold (start at 40KB), summarize
   the older half into a `# Compacted <date>` section and keep
   only the tail verbatim. Use `fitt-fast` for the summarization
   call. Include an operator-customisable
   `memory.compaction_prompt` that defaults to "preserve
   decisions, file paths, rules, user corrections; summarize tool
   results." Estimate: a week. Biggest Problem B win.

6. **Eval harness.** A small pytest-style runner that hits each
   alias with a curated set of tool-use prompts (read a file, run
   a command, schedule a cron, ask a capability question). Pass/
   fail per alias. Output lands in
   `$FITT_HOME/eval/alias-report.md`. Same mechanic as the boot
   check but with real coverage. Cost: a few days; pays for
   itself the first time it catches a model swap that breaks
   tool-use.

   *Shipped 2026-05-11.* `gateway/src/gateway/alias_eval.py`
   provides `EvalCase` / `CaseResult` / `EvalReport` dataclasses,
   `run_eval_case` / `run_eval_suite` runners, and
   `render_report_markdown` / `write_report` persistence. Reuses
   the shape-level classification primitives from `alias_probe`
   (`extract_tool_calls`, `assistant_message_from_response`,
   `response_to_dict`) so the harness and the probe agree on
   what "narration" means. The starter suite of 5 cases covers
   baseline tool call, different-tool-different-args,
   two-tool disambiguation, negative-case-small-talk, and
   list_capabilities-with-no-args. `fitt eval alias <name>`
   runs the suite with an optional `--min-pass-rate` CI gate;
   `fitt eval all` loops across every configured alias. Reports
   land at `$FITT_HOME/eval/<alias>-<timestamp>.md` (audit
   trail) and `$FITT_HOME/eval/<alias>-latest.md` (rolling
   per-alias). 15 tests cover per-case classification
   (pass / wrong_tool / narrated / truncated / transport_error
   / no_tool_expected_but_called), suite aggregation,
   report rendering, and rolling-vs-timestamped persistence.

7. **Shape-level narration signal (replaces the doomed regex).** A
   turn-level heuristic: model emitted `finish_reason=stop`, no
   `tool_calls`, reply is over N characters, AND the user's
   original message triggered tool-calling expectations (presence
   of `tools` / `tool_choice` in the request). Emit a
   `tool_expected_none_called` event. Don't match on content; the
   pattern is *structural*. Operator can look at
   `fitt inbox --kind tool_expected_none_called` to see how often
   their current alias is failing the channel. Cost: a couple of
   days; feeds the eval harness.

What's explicitly **not** on the list:

- Regex matching on `TOOL_NAME:/BEGIN_ARG:/END_ARG`.
- Regex matching on ```` ```json\n{"name": ...\n``` ````.
- Regex matching on `<tool_call>...</tool_call>` literal text.
- Regex matching on any specific hallucination shape.
- Skip-persistence logic gated on those regexes.

We already shipped the ```` ```json ```` detector in Phase 4
(`detect_narrated_tool_call` in `capabilities.py`) and used it for
the `tool_call_narrated` event. That was a mistake with the same
shape as the sentinel one we almost repeated. It should be removed
and replaced by (7), the shape-level signal. Do that as part of
the next phase rather than thrashing now.

## Related FITT principles this survey reinforces

- **Principle 3:** "Use mature tools; don't reinvent." Compaction is
  mature; Claude Code and Cursor both ship it. Port the pattern,
  don't invent a new one.
- **Principle 7:** "Models are configuration, not architecture."
  Eval harness enforces this: model choice is visible and
  measurable, not wired into code paths.
- **Principle 8:** "The agent is honest about its capabilities."
  Surfacing `tool_expected_none_called` in the inbox is honesty
  about when the agent failed to use the channel. The event
  stream (Problem D) is the same principle applied to the turn
  in progress: honesty about what the agent is doing *right now*,
  not just after the fact.
- **Principle 11:** "Fail loud on detectable misconfigurations."
  Boot-time alias validation, not regex.

## Related roadmap phases

- **Phase 5 (Lessons + decaying history)** ships decay and tool-
  turn persistence. This doc extends that direction: next stop is
  compaction and tool-output disk persistence.
- **Phase 7 (Memory v1)** was already framed as "RAG, compaction,
  cross-project." This doc reframes compaction as Phase 5.5 or 6.5
  rather than waiting for Phase 7; the need is immediate.
- **Principle 11 backlog item** (boot-time warning for shaky alias
  binding) should be promoted to a concrete task under Phase 1.5 or
  as a standalone mini-phase. It's ~hours of code.
- **Visibility (Problem D, action 3)** doesn't fit cleanly into an
  existing phase — it cuts across every phase that touches the
  agent loop. Worth a standalone mini-phase between current work
  and Phase 6 (Autonomy), since autonomy without visibility turns
  cron firings into more black boxes.

## Open questions

1. Where does the eval harness live? Own package (like
   `telegram-bot/`), a subcommand of the `fitt` CLI
   (`fitt eval alias fitt-smart`), or a separate binary? Leaning
   CLI subcommand for now; can split if it grows.
2. What's the compaction prompt's default shape? Claude Code's
   default is a generic "summarize"; users universally override it.
   We should ship something opinionated (preserve decisions, file
   paths, rules; drop tool results) and let operators override via
   config.
3. How does compaction interact with lessons? Lessons are already a
   form of curated summary. Maybe compaction is the *automatic* path
   and lessons are the *operator-curated* path; they share the same
   storage shape but different trust levels.
4. What about multi-agent? The `tool_poisoning` attack vector
   ([Red Teaming MCP Tools](https://arxiv.org/html/2509.21011)) is
   a security problem, not reliability, and out of scope here. Flag
   for Phase 10 (hardening).
5. How do we render the event stream on Telegram given no native
   streaming? One message per step is noisy; a single rolling
   edit-in-place message hits Telegram edit rate limits on long
   turns. Claude Code bots in the community converge on "rolling
   edit with coarse updates plus a final collapse into tool
   summaries," but we should prototype before committing to a
   pattern.
6. Do we build visibility before or alongside receipt cross-
   checking? They reinforce each other (the event stream makes
   receipts naturally visible; receipts give the stream
   something truthful to render). Loose coupling is probably
   right: ship the stream schema first, cross-checking and
   rendering both land later and read from it.
7. Out of scope for this doc but worth flagging: the bigger
   architectural question of whether FITT should wrap a coding
   CLI (MeshClaw-pattern) rather than build its own coding-agent
   layer. That's a roadmap conversation with its own doc. The
   fixes proposed here apply to the current architecture and
   don't lock us out of that direction — compaction, receipts,
   visibility, alias eval are all orchestrator-level concerns
   that transfer cleanly to a wrapping layer.

## Sources

Content across cited sources was rephrased for compliance with
licensing restrictions.

- [Context Poisoning in Long-Running AI
  Agents](https://tianpan.co/blog/2026-04-15-context-poisoning-
  long-running-agents) — the best single framing of Problem B.
- [Claude Code Compaction: How Context Compression Works
  (2026)](https://okhlopkov.com/claude-code-compaction-explained/)
  — operator-level reality check.
- [Context Compaction in Claude Code: A Five-Layer
  Cascade](https://finisky.github.io/en/claude-code-context-
  compaction/) — engineering detail.
- [Cursor Summarization
  docs](https://docs.cursor.com/en/agent/chat/summarization).
- [Cursor Dynamic Context
  Discovery](https://cursor.com/blog/dynamic-context-discovery).
- [Claude context editing
  docs](https://platform.claude.com/docs/en/build-with-
  claude/context-editing).
- [Practical Hallucination Detection for AI
  Agents](https://arxiv.org/html/2603.10060v1) (receipts paper).
- [Failure Makes the Agent Stronger: Structured
  Reflection](https://arxiv.org/abs/2509.18847).
- [Feedback Loops With Language Models Drive In-Context Reward
  Hacking](https://arxiv.org/html/2402.06627v3) — why "bigger
  model" and "better prompt" aren't solutions.
- [Reliable Tool-Using AI Agents in
  Production](https://labs.adaline.ai/p/reliable-tool-using-ai-
  agents-production) — runtime-level framing.
- [Continue Agent mode
  docs](https://www.continue.dev/docs/ide-extensions/agent/how-it-
  works) — what our IDE client actually does.
