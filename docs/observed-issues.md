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
