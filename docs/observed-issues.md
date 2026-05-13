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

## FITT capability block leaks into coding-CLI clients (Aider)

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
FITT-as-router for coding-CLI tools (Aider today; Claude
Code, Cursor, Continue-Agent, Codex, Kiro-CLI tomorrow). At
minimum: one wasted turn per session chasing a ghost tool
list. Worst case: the model pattern-matches on FITT's `ssh`-
routed file tools and tries to call them instead of Aider's
own file edits, which silently breaks the Aider workflow.

**Fix plan:** Router-mode for coding-CLI clients. Classify
clients via `X-FITT-Client` (values `aider`, `claude-code`,
`cursor`, `codex`, or the generic `coding-cli`). When the
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

**Fix landed 2026-05-11:** A new `coding-cli` client tag joins
`{ide, telegram, webui, cli}` as an accepted value for
`X-FITT-Client` and the `client:` field on tokens. Single
source of truth lives at `gateway.auth.is_router_mode_client()`;
`chat.py`'s chat handler calls it once at request entry and
branches:

* Router mode (`coding-cli`): skip memory load, skip
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
`X-FITT-Client: coding-cli` to Aider's `extra_headers` config,
or tag the Aider token with `client: coding-cli` in
`secrets.yaml`.

**Reshape 2026-05-13:** The `coding-cli` value conflated two
orthogonal concepts: which client interface the request came
from (a noun) and what runtime mode FITT should run (a
behaviour). Calling the IDE-shaped surfaces "coding-cli" worked
by accident because every router-mode caller happened to be a
coding CLI, but it ruled out useful combos like "Telegram in
router mode for an experiment" or "VS Code chat in agent mode
vs OpenCode in router mode through the same `client: ide`
policy bucket".

Split into two fields on `allowed_tokens`:

* `client:` — `ide | telegram | webui | cli`. The request's
  interface, drives per-client policy and routing.
* `mode:` — `router | agent`. The runtime layering. Default
  `agent` (full FITT). Set `router` for thin pass-through.

Recommended setup for OpenCode, Aider, Claude Code, etc.:
`client: ide` + `mode: router`. The legacy `client: coding-cli`
still parses; the runtime maps it to `client: ide, mode: router`
with a boot-time `auth.coding_cli_tag_deprecated` log line
prompting the operator to migrate. `is_router_mode_client(client)`
is gone; replaced by `is_router_mode_request(request)` which
reads the new `request.state.mode`.

`X-FITT-Client: coding-cli` on the request header is no longer
accepted (the value isn't in `_VALID_CLIENTS`). Setting router
mode is now a token-level decision, not a per-request one —
makes sense because mode is an authority decision the operator
takes when issuing the credential, not something the request
gets to declare.

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
