# Prior Art

Projects in the same design space as FITT — self-hosted
personal AI assistants, LLM routers, and agent frameworks.
Not a buyer's guide. A watch-list: things we've looked at,
what we'd borrow, what we wouldn't, and why FITT still
exists separately.

Reviewed when a new project in this space surfaces and we
need to decide whether to adopt, fork, or crib. Keep it
short; link rather than summarize where a proper source
exists.

## Why this doc exists

FITT started with a working principle (number 3): *use
mature tools; don't reinvent.* That's how we ended up on
LiteLLM instead of hand-rolling model routing. The same
rule ought to apply as the ecosystem grows — when someone
ships good open-source code for a thing we were planning
to build, we should at least consider leaning on theirs.

But "keep up to date" is a losing battle when done
ad-hoc. Several 2026 projects showed up in conversation
this month (MeshClaw, OpenClaw, Hermes) and we had to
rediscover the same tradeoffs each time. This doc is the
place that memory lives.

Scoped to projects we've actually looked at or used.
Curated, not exhaustive.

## What we've already adopted

- **LiteLLM.** Gateway dispatch. Handles OpenRouter /
  Ollama / OpenAI-compatible endpoint routing, fallback,
  basic observability. Replaces the entire "translate
  between provider APIs" problem. Principle 3 win.
- **FastAPI + Uvicorn.** HTTP layer. Standard choice.
- **python-telegram-bot (PTB).** Telegram integration.
  Standard choice for async Telegram bots in Python.
- **uv.** Python tooling.
- **Pydantic.** Config + request validation.

## Reference systems we've studied

### MeshClaw (Amazon internal)

*What:* 24/7 autonomous AI platform for Amazonians,
powered by `kiro-cli` underneath. Adds persistent memory,
cron autonomy, multi-channel (Slack, dashboard, TUI),
subagents, self-learning on top.
*Relevance to FITT:* closest architectural match we've
seen. Demonstrates the wrap-a-coding-CLI pattern at scale,
validates that "orchestrator owns memory/crons/surfaces
while a dedicated CLI owns the coding loop" is a shape
that works.
*Why not adopt:* Amazon-internal; Amazon-specific tooling
(Brazil, Midway, Slack, Taskei); not publicly available.
*What to borrow:* the pattern. Specifically the three-
autonomy-modes framing (scheduled / proactive /
reactive), the subagent orchestration model, and the
LLM-compressed-history approach to session restarts. See
earlier conversations for deeper analysis.
*Cross-reference (2026-05-15):* OpenClaw's heartbeat /
SOUL.md / TOOLS.md naming convention strongly resembles
MeshClaw's. Whether one inspired the other or both
converged on the same shape is unclear from outside;
either way, the convergence is worth respecting — when
two unrelated 2026 projects pick the same names for the
same concepts, those names are a small Schelling point
worth adopting.

### OpenClaw (Peter Steinberger, now OpenAI-sponsored)

*What:* Self-hosted personal AI assistant in
TypeScript. Local gateway + 23 messaging channels
(WhatsApp, Telegram, Slack, Discord, Signal, iMessage,
Matrix, etc.) + skills system + workspace +
companion mac/iOS/Android apps. MIT license.
Creator joined OpenAI Feb 2026; project moved to an
independent foundation with OpenAI as financial sponsor.
*Relevance to FITT:* uncomfortably close pattern match.
Same local-first, markdown-driven-config, per-session
memory, skills-as-markdown-dirs shape we converged on
independently. Six to twelve months further along on most
surfaces.
*Why not adopt:* TypeScript base; sprawling scope
(channels, voice, Canvas, multi-agent routing) compared
to FITT's narrow Telegram + gateway + memory. Opinions
not all ones we share — OpenClaw is reactive (DMs in,
agent out) where FITT's vision leans proactive
(scheduled + heartbeat + cross-surface persistence).
Switching means inheriting a much bigger codebase than
one person can comfortably maintain.

### OpenClaw — 2026-05-15 problem-driven audit

After installing OpenClaw alongside FITT on the same
NAS, this is the source-level read. Repo cloned to
`.scratch/openclaw/` for reading; not a fork, just a
read-only checkout for comparison.

Organized around questions FITT has actually hit
during development. Each entry: what FITT does, what
OpenClaw does, whether their approach is better
worse or just different, and whether to borrow.

#### Q: How do they handle Telegram approvals?

*FITT today:* one renderer per turn (Phase 4.8b
fixed the double-bubble bug); approval is a separate
inline-keyboard message; ApprovalPoller owns the
approval bubble lifecycle; gateway pushes
`approval_pending` events; bot edits-in-place on
decision.

*OpenClaw:* `extensions/telegram/src/approval-*.ts`.
Architectural pieces:
- A `telegramApprovalNativeRuntime` built on
  `createChannelApprovalNativeRuntimeAdapter` (a
  generic plugin-SDK shape — channels implement
  `presentation` + `transport` + `availability` +
  `shouldHandle`).
- Inline keyboard buttons sent with `callback_data`
  payloads.
- Telegram's hard 64-byte `callback_data` limit is
  handled explicitly: `approval-callback-data.ts`
  has `fitsTelegramCallbackData` and
  `rewriteTelegramApprovalDecisionAlias` to fit
  long approval IDs by truncation/aliasing
  (e.g. `allow-always` → `always`).
- "Edit reply markup" used to update buttons after
  decision (same pattern as FITT).

*Comparison:* same shape conceptually. Their
plugin-SDK adapter is the difference — they
abstract approval handling so any channel
implements the same contract; FITT has hand-rolled
Telegram-specific logic. **Worth knowing about**:
the 64-byte callback_data limit and their alias
rewrite pattern. FITT's approval-id is a UUID, so
"plugin:<uuid>" → 8 + 36 = 44 bytes for the id
alone; we'd hit the limit if we ever encoded more
than `<id> <decision>`. Worth a note in the
approval design doc.

*Borrow:* the alias-rewrite pattern if we ever
extend the callback payload. Don't borrow the
plugin-SDK abstraction — it's overkill for
single-channel.

#### Q: How do they handle upstream timeouts vs cancellation?

*FITT today:* Phase 4.9. Gateway-side
`asyncio.wait_for(asyncio.shield(task), timeout=300)`.
The `shield` keeps the orphan task alive after our
wait_for fires; we deliberately do NOT cancel.
LiteLLM ignores its own `timeout=` kwarg
(observed 2026-05-13: 300s configured, took 903s
to raise) which is why we wrap it in our own
`wait_for` at the router layer.

*OpenClaw:* uses `AbortController` / `AbortSignal`
end-to-end. Tests like
`anthropic-transport-stream.test.ts::"cancels
stalled SSE body reads when the abort signal fires
mid-stream"` and `"treats already-aborted signals
as abort errors before reading SSE chunks"`
confirm cancellation actually propagates to their
custom HTTP transport and closes the upstream
connection. They don't use LiteLLM (see below);
they wrote their own provider transports
(`*-transport-stream.ts`) and own the cancellation
contract.

*Comparison:* they cancel for real, we orphan-and-
shield. Both decisions are defensible:
- They paid for a hand-written transport layer to
  control cancellation. Tradeoff: every provider
  API change is theirs to track.
- We chose LiteLLM-as-mature-tool (principle 3),
  inheriting its bugs in exchange for not
  maintaining the layer.

*Borrow:* nothing concrete. The wait_for+shield
contract works for our scope. **Note for the
record**: if FITT ever moves off LiteLLM
(unlikely), we'd inherit the cancellation power
they have but also the maintenance cost.

#### Q: Do they use LiteLLM?

*FITT:* yes, for everything.

*OpenClaw:* no. Direct provider SDKs (`openai`,
`@google/genai`) plus hand-written transports for
Anthropic and others. Their `auth-profiles` system
manages multiple keys per provider with cooldown,
rotation, and `markAuthProfileFailure` — features
LiteLLM provides for free in our stack.

*Comparison:* they paid for the layer; we didn't.
A real architectural trade. Worth noting only so
that future-FITT thinking about provider-specific
features (auth rotation, model discovery probes)
checks LiteLLM first before re-implementing.

#### Q: What does their web UI have that a FITT dashboard would want?

*FITT today:* no web UI. CLI (`fitt watch`,
`fitt cost`), HTTP read endpoints (Phase 4.8c:
`/v1/events`, `/v1/audit`, `/v1/capability-gaps`,
`/v1/sessions`).

*OpenClaw views (`ui/src/ui/views/`):* `overview`,
`sessions`, `cron`, `agents`, `skills`,
`channels.{discord,slack,telegram,...}`,
`exec-approval`, `command-palette`, `chat`,
`debug`, `logs`, `usage`, `usage-metrics`,
`config`, `nodes`, `realtime-talk-*`, `dreaming`.
About 80 view files total.

*Comparison:* their UI is a real product, not just
an admin dashboard. Includes things FITT
specifically chose not to build (multi-channel
config, voice WebRTC, companion-app management,
Canvas).

*Borrow if/when FITT ever builds a dashboard:*
- `overview` with "is FITT okay right now"
  attention items
- `sessions` (browse history per session)
- `cron` (list/edit)
- `exec-approval` (browser-based approval queue)
- `logs` (event log viewer)
- `usage` (cost/token aggregation over time)

That's 6 views. The Phase 4.8c HTTP endpoints
already provide the API; the dashboard is a
consumer. **Effort estimate for a v1 minimal
dashboard**: 2-3 days. Don't build until operator-
pain calls for it.

#### Q: How do they "talk you through setup"?

*FITT today:* doesn't. Operator sets up via
docs + config files. The agent doesn't have a
"help me configure X" capability because the
docs aren't structured for the agent to drive.

*OpenClaw:* skills include setup recipes. Example:
`skills/gog/SKILL.md` has `Setup (once)` section
with literal commands; the agent reads the skill
markdown and walks the user through. The wizard
flow (`src/wizard/`) is for first-run (channel
config, model picker); skills cover ongoing
"add a new thing." Per-skill metadata
(`requires.bins`, `install.brew`) lets the agent
say "you'd need to install gog with `brew
install steipete/tap/gogcli`."

*Comparison:* their answer is just "agent reads
docs that were written agent-first." Same
substrate FITT has (system prompt injection); they
authored more content. Not a code gap, a content
gap.

*Borrow:* the pattern. If we ever want
"FITT, help me set up X" for an X we have, write
a markdown recipe addressed to the agent and
inject it. ~half a day per recipe; opens up
`gh auth login`, gog OAuth, project_shell project
setup, etc.

#### Q: How do they handle hallucinated tool calls?

*FITT today:* limited handling. The agent loop
expects structured `tool_calls`; if the model
emits text that *looks like* a tool call but
isn't structured, we treat it as final reply.
This was Problem A in
`docs/hallucinations-and-poisoning.md`.

*OpenClaw:* `src/agents/pi-tool-definition-adapter.ts`
+ `pi-tools.before-tool-call.*` files. They have
explicit before-call hooks that can repair or
reject malformed tool calls before execution,
and `tool-loop-detection.ts` for
detecting loops. Plus
`compaction.identifier-policy.test.ts` and
`compaction.tool-result-details.test.ts` —
serious infrastructure for tool-result cleanup
and identifier preservation across compactions.

*Comparison:* they've put real engineering into
this; we've punted. The hallucinations doc lists
this as Problem A and notes it's a real but
infrequent failure mode for us.

*Borrow if:* hallucinated-tool-call rate becomes
operationally annoying. The before-call hook
pattern (validate tool call shape before dispatch
to the tool registry) is portable to FITT's
agent loop with modest effort.

#### Q: How do they handle long histories / context budget?

*FITT today:* `MemoryStore` injects identity +
history + lessons. Phase 5 adds decay (history
older than N days drops from the prompt). Phase 4
hoists tool outputs >8KB to disk artifacts.
Compaction is not implemented.

*OpenClaw:* `src/agents/compaction.*.ts` (12+
files). They actively summarize long histories
into compressed forms when context runs out.
`compaction-real-conversation.ts`,
`compaction.summarize-fallback.test.ts`,
`compaction.tool-result-details.test.ts`. This
is a serious feature — they're in the
"context-engineering" deep end.

*Comparison:* FITT has the easier problem (one
user, narrow tool set, short sessions). We
haven't hit the limit yet. They've hit it
repeatedly enough to build a compaction system.

*Borrow:* not yet. Track in Phase 7 (memory v1).
When FITT hits the wall, look at their
`compaction.summarize-fallback.test.ts` for the
shape of "summarize older turns to free
budget."

#### Q: Do they have a heartbeat / proactive subsystem?

*FITT today:* `cron_*` tools. Operator (or model
via `cron_add`) schedules a session to fire later;
the agent runs and may call `send_message` if
something noteworthy. No heartbeat-shaped poll
loop.

*OpenClaw:* `src/auto-reply/heartbeat.ts` +
`src/cron/heartbeat-policy.ts`. Structured:
- Default prompt: "Read HEARTBEAT.md if it
  exists. Follow it strictly. If nothing needs
  attention, reply HEARTBEAT_OK."
- Model responds with a `heartbeat_respond` tool
  call: outcome ∈ {progress, no_change, done,
  needs_attention}, plus `notify=true|false`,
  optional `priority` low/normal/high.
- The framework decides whether to actually ping
  the user based on the model's `notify`
  decision.
- Default interval `30m`, configurable.
- Filtering: `heartbeat-filter.ts` strips
  heartbeat self-messages from the transcript
  the next agent turn sees.

*Comparison:* their model is "agent self-checks
on a timer; only interrupts the user when it
decides to." FITT's cron model is "agent does
something on a timer; can call send_message
freely." Their model is more discoverable
(HEARTBEAT.md is the obvious place to write
"check this every 30m") and less interrupting
by default (the `notify=false` default makes
it conservative).

*Borrow:* the schema —
`outcome ∈ {progress, no_change, done,
needs_attention}` + `notify=bool` + `priority`
— could be a thin layer on top of FITT's existing
cron + send_message. The HEARTBEAT.md filename
convention is a clean entry point. Effort: 1-2
days for a real heartbeat module that wraps
existing cron infrastructure.

#### Q: Do they sandbox sub-agents?

*FITT today:* no. Tools run on the host. Approval
flow is the only defense.

*OpenClaw:* `src/sandbox/` directory + per-agent
sandbox config. Backends include Docker (default),
SSH, OpenShell. Per-agent scope: the main agent
runs on the host, sub-agents run in fresh sandbox
containers. Workspace mount is
configurable (`workspaceAccess: "none"`).

*Comparison:* genuine security gap. FITT relies
on approval-gating + the operator paying
attention. They have defense-in-depth.

*Borrow if:* we ever ship sub-agent execution.
Today FITT doesn't have sub-agents (cron
firings reuse the main session in the same
process). When we do, sandboxing is the right
default.

#### Q: How do they manage multi-key auth rotation?

*FITT today:* one key per provider in
`secrets.yaml`. LiteLLM doesn't rotate. If a key
fails, the request fails.

*OpenClaw:* `src/agents/auth-profiles.*` —
multi-key per provider, cooldown on failure,
`markAuthProfileFailure`, automatic next-key
selection, configurable usage stats per profile.
`auth-profiles.cooldown-auto-expiry.test.ts` is
a load-bearing test.

*Comparison:* they need this; their users have
multiple OAuth identities (personal + work
GitHub Copilot, multiple OpenRouter accounts,
etc.). FITT is single-user and rarely runs into
key-level issues.

*Borrow:* not in scope for FITT. Note for the
record so future-FITT doesn't reinvent if a
specific need surfaces.

#### Q: What about web search, voice, RAG memory?

*FITT today:* none of these.

*OpenClaw:* yes to all. `src/web-search/`,
`src/realtime-transcription/`, `src/tts/`,
`src/memory/` (with vector search via embeddings).
58 bundled skills, ~20 of which depend on
external CLIs that wrap APIs.

*Comparison:* these are FITT's roadmap Phase 7-8.
Not gaps; just unbuilt.

*Borrow if/when:* the skills loader (the most
re-usable thing in this audit) makes a lot of
their voice/search/rag plumbing portable, since
each is mostly a CLI under the hood.

#### Q: How do they handle model-per-task vs "one entity"?

*FITT today:* clients name aliases
(`fitt-default`, `fitt-smart`, `fitt-fast`); the
gateway resolves the alias to a concrete model.
The user picks per-turn (Continue lets you flip
modes; Telegram defaults to one alias). Cron jobs
can specify `agent_alias`. No per-turn reasoning
knob.

*OpenClaw:* one default model
(`agents.defaults.model.primary` + optional
`fallbacks`) handles every chat turn. The user
doesn't pick. Per-turn knobs change *how the
same model thinks*, not which model:
- `thinking` / `reasoning` level
  (minimal/low/medium/high/xhigh) for adaptive-
  thinking models (Sonnet 4.5+, o-series).
- `fastMode` boolean for "skip the heavy
  thinking on this turn." Resolves session >
  agent > config-per-model > default.

Tiny task-specific model overrides for non-chat
work:
- `agents.defaults.compaction.memoryFlush.model`
  — cheaper model just for context summarization.
- `channels.<channel>.tts.summaryModel` — voice
  summary generation.
- `hooks.gmail.model` — Gmail-watcher hooks.

No per-tool model routing. Calling `gog calendar
create` and `read_file` both go through the chat
model.

*Comparison:* different philosophies for different
deployments.
- OpenClaw: "one entity, framework hides the rest."
  Right for messaging UX where the user shouldn't
  know what model is doing the work.
- FITT: "user names a role; gateway resolves."
  Right for IDE / developer workflows where
  explicit fast-vs-smart toggling is desirable.

Both are defensible for their target audience.
Don't change FITT's aliasing model.

*Worth borrowing:*
- **Per-turn `thinking` / `reasoning` level.**
  When the underlying model supports adaptive
  thinking (current Anthropic, Codex/o-series),
  letting the user say "think harder this turn"
  without changing model is more powerful than
  alias-swapping. ~half a day to surface in the
  request body. Note: needs LiteLLM to pass it
  through verbatim, which it does for known
  fields.
- **Task-specific model overrides
  (compaction.model, tts.model).** When FITT
  eventually ships compaction (Phase 7) or voice
  (Phase 8), follow this convention: separate
  model knob per task, defaulting to the primary
  alias but overridable. Saves cost on
  housekeeping (don't burn Sonnet credits on a
  50-token summary) without complicating chat.

#### Things FITT has that OpenClaw doesn't

For balance, things in FITT that didn't have
OpenClaw counterparts when I looked:

- **Capability-gap log** (`capability_gaps.jsonl`).
  Real-time "I'd need a tool to X" feed from
  agent failures. They have wizard-time
  `skills-status.test.ts` ("eligible / missing
  requirements") which is a snapshot, not a feed.
- **HMAC-chained audit log**. Tamper-evident
  forensic trail. They have logs but not a
  chained one.
- **Hub/Compute fallback router**. Multi-machine
  topology. Their assumption is single-host.
- **The Phase 4.8 growing-bubble Telegram
  renderer** is sharper than their Telegram
  rendering, which is closer to "post one reply
  when done."

#### Summary

The audit's actual takeaway: **most of the OpenClaw
gap that bothered us isn't architectural — it's
content (skills authored agent-first) and a few
specific patterns worth borrowing (heartbeat
schema, hallucinated-tool-call before-hooks,
callback-data alias rewrite for Telegram).** Their
hand-written provider transport is more powerful
but is paying for a feature FITT explicitly chose
to outsource to LiteLLM. Their web UI is real
work (~80 views) and not on FITT's path right
now.

The borrow-list is: skills loader (~half day),
heartbeat structured-outcome schema (1-2 days when
trigger arrives), Telegram callback-data alias
rewrite pattern (note for if/when we extend),
agent-first setup recipes for existing capabilities
(opportunistic, half-day each).

*Status:* [openclaw/openclaw on
GitHub](https://github.com/openclaw/openclaw). Active,
247k+ stars as of March 2026. MIT licensed. Covered in
Wikipedia.
*Watch for:* foundation governance post-acquisition
(will OpenAI keep sponsoring? who maintains it?).

### Hermes Agent (Nous Research)

*What:* Self-hosted, model-agnostic AI assistant in
Python. Runs on local machine, VPS, or serverless
infrastructure (Modal / Daytona / Vercel Sandbox).
Terminal (Ink TUI) + 20+ messaging surfaces (Telegram,
Discord, Slack, WhatsApp, Signal, Email, …). MIT licensed.
Built by Nous Research; closest explicit competitor
to FITT in shape and stack.
*Relevance to FITT:* very close. Same pitch ("install
on your server, give it your messaging accounts,
persistent personal agent"), same language, similar
patterns. Six months further along on the
self-improving / multi-channel / messaging-gateway axes.
*Why not adopt:* the same reasoning as OpenClaw —
sprawling scope (~17k tests across ~900 files,
"~12k LOC `AIAgent` class," `cli.py` "~11k LOC")
versus FITT's narrow Telegram + gateway + memory.
Inheriting that codebase means inheriting much more
than one person can maintain. Also, several of their
opinions diverge from FITT's (no aliasing, single
default model with `thinking`/`fastMode` knobs;
provider profiles instead of LiteLLM; closed in-tree
memory provider list with new ones forced to
out-of-tree plugins).
*Status:* [NousResearch/hermes-agent on
GitHub](https://github.com/NousResearch/hermes-agent).
Active development through May 2026. MIT licensed.

### Hermes Agent — 2026-05-21 problem-driven audit

Cloned to `.scratch/hermes-agent/` for source-level
reading; not a fork. Same problem-driven format as
the OpenClaw audit: each question is something FITT
hit during development, and the entry compares how
each project answers it.

#### Q: What does "self-improving skills" actually mean?

*FITT today:* `learn_*` tools — operator-correction
captures via `lessons/<project>.md`. The agent
references them on subsequent turns. No automatic
review, no archival, no consolidation. One-way
write.

*Hermes:* `agent/curator.py` (~1850 lines) +
`tools/skill_usage.py` + per-run reports.
Mechanism:
- Skills carry a `created_by: "agent"` provenance
  marker. Curator only touches agent-created
  skills; bundled and hub-installed skills are
  off-limits.
- `~/.hermes/skills/.usage.json` sidecar tracks
  per-skill `use_count`, `view_count`, `patch_count`,
  `last_activity_at`, `state`
  (active / stale / archived), `pinned`.
- `apply_automatic_transitions()` is a pure function
  (no LLM): walks skill records, marks `stale` after
  30 days idle, archives after 90 days idle.
  Reactivates a stale skill if it's used again.
- `run_curator_review()` then forks a separate
  `AIAgent` against an auxiliary model (cheap one,
  configured via `auxiliary.curator.{provider,model}`)
  to review the candidate list. The fork has
  `skip_memory=True` and disabled nudges so it
  doesn't recurse. It can pin / archive /
  consolidate / patch via `skill_manage`.
- Pre-run snapshot via `agent/curator_backup.py`
  (tar.gz of the skills dir). Reports written to
  `~/.hermes/skills/.reports/` per run with a
  rename map (old-name → umbrella) so the user can
  see what moved where. Archive is at
  `~/.hermes/skills/.archive/` and is fully
  restorable. Never deletes.
- Triggered inactivity-based: when the agent's
  been idle long enough and the last run was more
  than `interval_hours` ago (default 7 days),
  `maybe_run_curator()` spawns a daemon thread.
  No cron daemon; gateway-tick driven.

*Comparison:* this is real — pure-function transitions
+ LLM-judged consolidation + recoverable archive +
provenance-gated. Way past `learn_*`.

*Borrow:* not the whole thing. FITT's gap is content,
not architecture — we don't yet have enough lessons
or skills for archival to matter. The pattern worth
remembering: **provenance + sidecar telemetry + pure
transitions + LLM judge + recoverable archive**. If
Phase 7 (memory v1) ever needs a "stale lessons"
sweep, this is the reference implementation. Effort
to copy: real (multi-day). Don't until the pile is
big enough to be a problem.

#### Q: How is Honcho used for cross-session user modeling?

*FITT today:* memory is markdown-per-session
(`identity.md`, `today/<date>.md`). No structured
user model across sessions; no semantic search; no
"the assistant gets to know you over time" beyond
markdown the operator wrote by hand.

*Hermes:* `plugins/memory/honcho/` — a memory
provider plugin around the [Honcho](https://github.com/plastic-labs/honcho)
external service (plastic-labs). Implements
`MemoryProvider` ABC with five tools surfaced to
the model:
- `honcho_profile` — read/write a peer's "card"
  (curated facts).
- `honcho_search` — semantic search, returns
  excerpts ranked by relevance, no LLM synthesis.
- `honcho_reasoning` — natural-language Q&A
  against Honcho's dialectic reasoning model
  (higher cost; explicit `reasoning_level` knob).
- `honcho_context` — full session context
  retrieval (summary + peer rep + recent
  messages).
- `honcho_conclude` — write or delete persistent
  conclusions about a peer.
The provider's `sync_turn()` records each
conversation turn into Honcho non-blocking;
`prefetch()` retrieves relevant prior context for
the next turn's system prompt.

Honcho itself is the heavy work — a separate
project with its own dialectic reasoning model
that builds peer representations over time. The
Hermes plugin is a fairly thin wrapper.

*Comparison:* Honcho is the kind of "user
modeling layer" FITT would want for Phase 7
(memory v1, cross-project recall). It solves
a problem FITT explicitly has: "the assistant
should learn who I am over time without me
manually editing markdown." The fact that
Hermes integrates with it via a 1300-line
plugin and not by re-implementing means the
integration cost is portable.

*Borrow:* worth a real evaluation when Phase 7
starts. Honcho is open source, has an MIT-licensed
SDK, and runs as either a hosted service
(`app.honcho.dev`) or self-hosted. The plugin's
sync_turn / prefetch / system_prompt_block
contract is exactly the shape FITT's
`MemoryStore` would need. **Action:** when Phase 7
opens, look at `plugins/memory/honcho/__init__.py`
and `session.py` first. The five-tool surface
(`profile`, `search`, `reasoning`, `context`,
`conclude`) is a good schema even if FITT
doesn't end up using Honcho proper. Effort to
add Honcho-as-FITT-plugin: 1-2 days for a
working v0; more for the same care Hermes
gives it (reasoning-level knob, structured
peer aliases, etc.).

#### Q: Cross-session search — FTS5 + LLM summarization?

*FITT today:* no cross-session search. The agent
reads `identity.md` + today's history; older
sessions are markdown files in
`~/.fitt/today/<date>.md` that nothing reads after
the day rolls over.

*Hermes:* `tools/session_search_tool.py`. SQLite
FTS5 over the `~/.hermes/sessions.db` message
store. Three calling shapes from one tool:
- **Discovery** — pass `query`, get top-N
  matching sessions with a 5-message
  anchored window around the FTS5 hit, plus
  3-message bookends at session start/end.
- **Scroll** — pass `session_id +
  around_message_id`, get 5 messages either
  side of an anchor.
- **Browse** — no args, get recent sessions.
Honors lineage: session continuations are deduped
by parent so a single resumed conversation
doesn't appear N times. Excludes the current
session. Returns actual messages from the DB —
no LLM call in the search itself.

The README's "FTS5 + LLM summarization" claim:
the FTS5 is the search; the LLM summarization
is a separate feature
(`agent/conversation_compression.py`) that
compresses long histories into summaries when
context budget runs low. Two distinct things;
the README phrasing implies they're integrated,
but reading the code, they're not — search
returns raw messages and the agent does its own
summarization if needed.

*Comparison:* honest implementation. FITT will
need this when Phase 7 lands. The three-shape
contract (discovery / scroll / browse) is well-
designed — one tool, no proliferation.

*Borrow:* the schema. When FITT ships
cross-session search, the SQLite + FTS5 +
anchored-window pattern is the right one. One
tool, three shapes inferred from which args are
set. Effort: 1-2 days for an FTS5 layer over
a future FITT message store.

#### Q: Subagent spawning model — how isolated, how communicate?

*FITT today:* no subagents. Cron firings reuse
the main session. Tools run in the main process.

*Hermes:* `tools/delegate_tool.py`. Spawns child
`AIAgent` instances via `ThreadPoolExecutor`.
Two roles:
- `role="leaf"` (default) — focused worker.
  Cannot call `delegate_task`, `clarify`,
  `memory`, `send_message`, `execute_code`.
- `role="orchestrator"` — retains
  `delegate_task` for nested spawning. Gated
  by `delegation.orchestrator_enabled` and
  bounded by `delegation.max_spawn_depth`
  (default 2).
Two shapes: `goal=...` (single child) or
`tasks=[...]` (parallel batch, capped by
`max_concurrent_children`, default 3).
Key plumbing:
- Synchronous: parent blocks until children
  return.  Parent interrupt cancels children.
- Approval callbacks installed per-thread so
  children's dangerous-command approvals
  don't reach the parent's TUI (would
  deadlock). Default is auto-deny;
  `delegation.subagent_auto_approve=true`
  for opt-in YOLO.
- Process-global `_active_subagents` registry
  for live spawn-tree introspection (TUI
  observability layer reads it).
- Children inherit MCP toolsets opt-in
  (`inherit_mcp_toolsets`).
The "synchronicity rule" they explicitly
document: `delegate_task` is **not** durable.
For long-running work that must outlive the
turn, use `cronjob` or
`terminal(background=True, notify_on_complete=True)`.

*Comparison:* well-engineered for a single-
process agent. Some FITT-side problems they've
clearly hit (TUI deadlock from child approval
prompts) don't apply to FITT yet because we
don't have subagents.

*Borrow:* not yet. FITT's deployment shape
(Hub gateway + Compute Ollama; no Telegram-
spawned subagents on the to-do list) makes this
premature. **If/when** FITT ever wants
subagents, the toolset-restriction (`leaf` blocks
`send_message` so the child can't ping the user
on its own) and the per-thread approval-callback
plumbing are the patterns worth borrowing.
Effort to ship sub-agents would be 3-5 days
following their pattern.

#### Q: Seven terminal backends?

*FITT today:* `project_shell` runs locally on
the gateway machine, or via SSH to a registered
project's host (the Hub/Compute split). Two
"backends" in spirit: local and SSH.

*Hermes:* `tools/environments/` — `local.py`,
`docker.py`, `ssh.py`, `singularity.py`,
`modal.py` (Modal serverless), `daytona.py`
(Daytona dev sandboxes), `vercel_sandbox.py`,
`managed_modal.py`. Configured via
`terminal.backend` in `config.yaml`. The
"hibernates when idle" claim is real — Modal
and Daytona are both pay-per-second sandboxed
compute platforms. Hermes the agent can be
running on a $5 VPS while heavy `terminal()`
calls go to a Modal sandbox that wakes on
demand.

*Comparison:* Hermes has built up a real
abstraction here. FITT's SSH backend covers
the "remote execution" case (Compute) but
nothing serverless. The Modal/Daytona/Vercel
backends are paid services — outside Principle
5 (no subscription).

*Borrow:* maybe the SSH backend's auth path
if Compute ever needs more polish. Modal /
Daytona / Vercel — not for FITT. Singularity
(HPC-style container) is potentially relevant
if FITT ever runs on a research cluster but
that's not the deployment story today.

#### Q: How do they handle Telegram approvals?

*FITT today:* one renderer per turn,
`ApprovalPoller` owns the approval bubble,
inline-keyboard buttons with callback_data.

*Hermes:* `gateway/platforms/telegram.py` —
`send_exec_approval()`. Inline keyboard, four
buttons: Allow Once / Session / Always / Deny.
Compact `callback_data` of the form
`ea:<choice>:<approval_id>` where `approval_id`
is a small monotonic counter (so the 64-byte
limit is never an issue). Mapping from
`approval_id` → `session_key` is held in
`self._approval_state` (in-memory dict). On
button click: validates the user is
authorized (`_is_callback_user_authorized()`),
edits the message in place to show the
decision label, then calls
`resolve_gateway_approval(session_key, choice)`
to unblock the agent thread.

*Comparison:* nearly identical to FITT's
approval pattern. Differences:
- Counter for `approval_id` instead of UUID,
  which keeps `callback_data` short. (FITT
  uses UUID; ~44 bytes for the id alone.
  We're under the 64-byte limit but with no
  margin if the schema grows.)
- Per-user authorization check on every
  click. FITT relies on chat-level filtering.
  If a malicious user is somehow added to
  the chat, FITT's approval is openable by
  anyone in the chat.
- Four-button layout (Once / Session / Always
  / Deny) versus FITT's per-prompt button set.

*Borrow:*
- The compact `ea:<choice>:<id>` format with
  a counter — note for the future. Pattern
  same as the OpenClaw alias-rewrite
  recommendation. Cheap to add.
- The per-click `_is_callback_user_authorized`
  check — worth a Phase 4 hardening pass. If
  FITT's Telegram chat ever gains a second
  user (operator's family member, etc.) the
  approval should still be operator-only.
  Few hours.

#### Q: How do they handle upstream timeouts vs cancellation?

*FITT today:* Phase 4.9.
`asyncio.wait_for(asyncio.shield(task), timeout=300)`
at the router layer. We orphan-and-shield (don't
cancel the upstream call) because LiteLLM's
`timeout=` kwarg is unreliable.

*Hermes:* multi-layer.
`hermes_cli/timeouts.py` exposes
`get_provider_request_timeout` and
`get_provider_stale_timeout`. Per-provider
timeouts in config (e.g.
`models.providers.<id>.timeoutSeconds`).
`agent/error_classifier.py::FailoverReason`
classifies any TimeoutError /
APITimeoutError / connection error as
retryable timeout. The error message they
show is the actionable one we noted in the
OpenClaw audit ("increase
`models.providers.<id>.timeoutSeconds`…")
because both projects landed on similar
phrasing — Hermes's may be the original.
Cancellation: they don't use LiteLLM (see
below) so the OpenAI SDK's native timeout
semantics apply, and the code does cancel.
On non-streaming calls a watchdog raises
TimeoutError after the stale threshold and
the underlying request is closed.

*Comparison:* they have real cancellation
because they own the transport (no LiteLLM
in between to ignore the timeout kwarg).
Their error message UX is the same actionable
shape OpenClaw has. Both are better than
FITT's `upstream_silent` v0 message.

*Borrow:*
- The error-message phrasing — same as the
  OpenClaw note. "Better-shaped operator
  error messages" was already in the pick-
  list; Hermes's phrasing is another data
  point that the actionable-config-key
  format works.
- Provider-level timeout config keys (rather
  than gateway-wide), so ad-hoc local Ollama
  calls can have a longer ceiling than
  cloud calls. FITT today has one
  `upstream_timeout_secs`. Worth a Phase 5+
  config split: `providers.<id>.timeout_secs`.
  Half a day.

#### Q: Do they use LiteLLM?

*FITT:* yes, for everything.

*Hermes:* no. `plugins/model-providers/` has
**28 provider profiles** (anthropic, openai-codex,
gemini, deepseek, openrouter, novita, kimi,
nvidia, zai, xiaomi, minimax, bedrock, copilot,
nous, opencode, gmi, huggingface, kilocode, …).
Each is a `ProviderProfile` subclass that knows
the provider's quirks (Anthropic's reasoning
config, Kimi's omitted temperature, Gemini's
thinking_config translation, OpenRouter's
provider-preferences passthrough, Qwen's OAuth
and message normalization, Bedrock's lack of
`/v1/models`). Discovery is lazy at
`providers/__init__.py._discover_providers()`,
NOT through the general PluginManager (would
double-instantiate).

*Comparison:* same trade as OpenClaw — they paid
for the layer themselves. Bigger surface area
than OpenClaw because Hermes targets even more
providers (Nous Portal, Modal, Daytona, etc.)
out of the box. Hand-written per-provider
quirks visible in the codebase tell the
story: every provider has a different
reasoning shape and Hermes serializes them
all in profile classes.

*Borrow:* nothing concrete. FITT's
LiteLLM-as-mature-tool decision still makes
sense for our two-provider scope. The
provider-profile pattern is interesting if
FITT ever moves off LiteLLM, but that's a
~2-month migration we haven't decided to do.

#### Q: What does their dashboard / web UI have?

*FITT today:* no dashboard. CLI + HTTP read
endpoints (Phase 4.8c).

*Hermes:* `web/` (web app) + `hermes_cli/web_server.py`
(FastAPI server). Notable surfaces in
`web/src/pages/`:
- `ChatPage` — embeds the actual `hermes --tui`
  via xterm.js + WebSocket-PTY bridge. NOT a
  rewrite of the chat UX. The dashboard's chat
  is the same Ink TUI running in a remote
  terminal.
- Sidebar widgets (`ChatSidebar`,
  `ModelPickerDialog`, `ToolCall`) — supporting
  views, not a second chat surface.
- Various supporting pages: settings, logs,
  insights, sessions, cron, kanban.

The architectural rule they document
explicitly in AGENTS.md: **don't reimplement
the chat experience in React. Extend the Ink
TUI; the dashboard embeds it via PTY.**

*Comparison:* clever solution. They get a
modern web UI without building two chat
implementations. The cost: requires a POSIX
PTY (Windows works only via WSL2). FITT's
two-machine cluster has Linux on the Compute
node and Windows on the Hub; an embedded-PTY
dashboard would only work on the Linux
machine.

*Borrow:* the architectural principle. **If
FITT ever gets a dashboard, the chat pane
should not be a second implementation of
Telegram-style rendering.** Either embed a
TUI (cross-platform issue with Windows) or
make the dashboard the consumer-of-an-event-
stream that the existing renderer already
publishes. The latter fits FITT's existing
shape better (Phase 4.8c HTTP endpoints + the
Telegram renderer). Don't duplicate render
logic.

#### Q: How do they "talk you through setup"?

*FITT today:* same as OpenClaw answer — the
docs aren't structured for the agent to drive.

*Hermes:* same answer as OpenClaw. Skills are
markdown with prose, prerequisites, "Setup
(once)" sections, command examples, fallback
paths. `hermes setup` runs an interactive
wizard for first-time provider/model/messaging
config, but ongoing "help me set up X" comes
from the same skills-as-markdown substrate.
Plus `hermes claw migrate` for migrating from
OpenClaw — a recipe-driven import.

*Comparison:* same takeaway. **Content gap,
not architecture gap.** If FITT had a
skills loader (top of the OpenClaw pick-list),
both projects' skills would be drop-in usable.

*Borrow:* same as OpenClaw recommendation.
Skills loader is the single highest-leverage
change. Hermes adds an additional argument:
**two independent projects converged on
markdown-skills as the substrate.** Strong
signal that this is the right shape.

#### Q: How do they handle hallucinated tool calls?

*FITT today:* limited handling, same as the
OpenClaw answer.

*Hermes:* `agent/tool_executor.py` +
`agent/tool_dispatch_helpers.py` +
`agent/tool_guardrails.py` +
`agent/tool_result_classification.py`. They
have a `schema_sanitizer.py` that fixes
common malformed-schema issues before
dispatch. `tool_guardrails.py` enforces
toolset-level restrictions (leaf children
can't call `send_message`, etc.).
`error_classifier.py` distinguishes recoverable
errors from terminal ones. Like OpenClaw,
they've put real engineering into this.

*Comparison:* both reference systems have
defense-in-depth that FITT doesn't.

*Borrow:* same conclusion as OpenClaw. If
hallucination rate becomes operationally
annoying, the schema-sanitize-before-dispatch
pattern is the right shape. Until then, punt.

#### Q: How do they handle long histories / context budget?

*FITT today:* same as OpenClaw answer.
Compaction not implemented.

*Hermes:* `agent/conversation_compression.py`
(`compress_context()`),
`agent/context_compressor.py`,
`agent/context_engine.py`,
`agent/manual_compression_feedback.py`,
`agent/trajectory.py` +
top-level `trajectory_compressor.py`. They
also have a "model feasibility" check — refuses
to compress against a model that would
hallucinate the summary. Slash commands
`/compress`, `/usage`, `/insights` for
operator-driven compaction. Curator-driven
context-engine plugins
(`plugins/context_engine/`) for swappable
strategies.

*Comparison:* even more elaborate than
OpenClaw's compaction system. Real
infrastructure.

*Borrow:* same Phase 7 placeholder.
`conversation_compression.py` is a useful
starting reference if FITT ever needs to
ship compaction. Trajectory compression as a
side activity (used here for training data
generation) is interesting but out of FITT's
scope.

#### Q: Do they have a heartbeat / proactive subsystem?

*FITT today:* `cron_*` tools. Operator/agent
schedules sessions to fire later; the agent
runs and may call `send_message`. No
heartbeat-shaped poll loop.

*Hermes:* surprisingly, **no user-facing
heartbeat** in the OpenClaw sense (no
`HEARTBEAT.md` poll loop, no
`heartbeat_respond` outcome ∈ {progress,
no_change, done, needs_attention} contract).
What they call "heartbeat" is internal:
`kanban_db.heartbeat_claim()` and
`heartbeat_worker()` are worker-liveness
signals for the kanban dispatcher's
multi-worker board — completely different
domain. The proactive surface is `cron/`
+ webhook subscriptions:
- `cron/jobs.py` + `scheduler.py` — schedule
  formats include duration ("30m"), "every"
  phrases ("every monday 9am"), 5-field
  cron, ISO timestamps.
- 3-minute hard interrupt on cron sessions,
  catchup window, grace window — operational
  hardening FITT mostly has too.
- Webhook subscriptions
  (GitHub events, generic API triggers) are
  the "external trigger" piece FITT doesn't
  have.
- Pre-run scripts can inject context into
  the prompt; `[SILENT]` response suppresses
  delivery so the operator only gets pinged
  when something actually changed.

*Comparison:* OpenClaw's heartbeat pattern
isn't here. FITT's cron pattern matches
Hermes more closely (timer fires; agent
decides whether to ping). Both projects
treat the "wake up periodically and check
X" use case via cron, not via a separate
heartbeat subsystem.

*Borrow:*
- The `[SILENT]` response convention for
  cron jobs. Cheap and powerful. FITT's
  `send_message` is unconditional in
  cron firings — adding a "model says
  silent → don't notify" path costs almost
  nothing and prevents notification
  fatigue. Few hours.
- Webhook subscriptions are a real Phase 6+
  feature when FITT moves past polling. Not
  on the critical path now.
- The OpenClaw heartbeat schema (the
  `outcome ∈ {progress, no_change, done,
  needs_attention} + notify=bool +
  priority` contract) remains the right
  shape for FITT's eventual heartbeat —
  Hermes doesn't disagree with it, just
  doesn't have one.

#### Q: Do they sandbox sub-agents?

*FITT today:* no. Tools run on the host.

*Hermes:* yes, indirectly. Subagents run in
the same process by default (Python threads),
but the `terminal()` tool inside them
defaults to the per-agent
`terminal.backend` config — which can be
Docker, Modal, Daytona, or a sandbox.
Direct comparison to OpenClaw's per-agent
sandbox option:
- OpenClaw: explicit `workspaceAccess`
  per agent.
- Hermes: `terminal.backend` per profile;
  subagents inherit the parent's config
  unless overridden.

Less granular than OpenClaw's per-agent
sandbox. Stronger than FITT's "no sandbox."

*Comparison:* both reference systems offer
sandboxing FITT doesn't have. Both pay for
it via Docker / cloud sandbox boxes, which
collide with FITT's Principle 5.

*Borrow:* same conclusion as OpenClaw. Not
in scope without sub-agents or a real
operational driver.

#### Q: Multi-key auth rotation per provider?

*FITT today:* one key per provider in
`secrets.yaml`.

*Hermes:* `agent/credential_pool.py` +
`agent/credential_sources.py`. Multi-key
support with cooldown and rotation, similar
to OpenClaw's `auth-profiles` system. Same
trade-off: FITT's single-user scope doesn't
need it.

*Borrow:* not in scope.

#### Q: How do they handle web search?

*FITT today:* none.

*Hermes:* `agent/web_search_provider.py`
(ABC) + multiple providers in
`plugins/web/`: brave-free, ddgs, searxng,
exa, parallel, tavily, firecrawl. The
search_backend / extract_backend split
matches the FITT roadmap intuition (web
search is one capability; web extract is
another). The DDGS (DuckDuckGo HTML scrape)
provider is the no-key path; SearXNG is the
self-hosted one — exactly the two FITT
identified earlier in this doc as the
right options.

*Comparison:* Hermes ships the option set
FITT planned to build. The
`WebSearchProvider` ABC contract
(`search` / `extract` / `crawl` capability
flags, plus `is_available` / `get_setup_schema`
for `hermes tools` picker integration) is
clean and portable.

*Borrow:* the ABC shape, when FITT ships
web search. Five method names + capability
flags + a config-driven backend selector.
The skills-loader path (per the OpenClaw
pick-list) is one way; this provider-ABC
path is another. Both are defensible. Effort
either way: half a day.

#### Q: Per-turn `thinking` level vs alias swap?

*FITT today:* alias-per-role. No per-turn
reasoning knob.

*Hermes:* same as OpenClaw answer — single
default model + per-turn `thinking` /
`reasoning_level` knob (where the underlying
provider supports it: Anthropic, Codex/
o-series, DeepSeek, Gemini). The
`agent/portal_tags.py` translates the tag
across providers. Per-task overrides for
auxiliary work (curator, vision, embedding,
title generation, session_search) sit
under `auxiliary.<task>.{provider,model,
api_key,base_url,extra_body}`.

*Comparison:* both Hermes and OpenClaw
converged on the same answer here. FITT's
alias-per-role is the better fit for IDE
workflows; the per-turn `thinking` and
auxiliary-task overrides are still worth
borrowing in the same form OpenClaw uses.

*Borrow:* same conclusion. Per-turn
`thinking`/`reasoning_level` ~half day.
Task-specific model overrides for
compaction/voice/curator-style tasks ~half
day each, lands with whatever phase needs
the task.

#### Q: Does Hermes use python-telegram-bot like FITT?

*FITT:* yes.

*Hermes:* yes. Same library, same callback-
driven dispatch, same async/await model.
Their integration is more elaborate (model
picker UI in inline keyboards, sticker
caching, "memory mode" for browsing past
turns) but the foundation is the same. Two
independent projects landed on PTB —
another small Schelling point.

*Borrow:* nothing — we're already there.

#### Things FITT has that Hermes doesn't

For balance:

- **Aliases** as a routing concept. Hermes
  has providers + models; FITT has
  `fitt-default` / `fitt-smart` / `fitt-fast`
  bound by config. Better fit for IDE
  workflows where the user explicitly says
  "use the smart one for this turn."
- **Hub/Compute fallback router**.
  Multi-machine topology with a fallback
  contract. Hermes assumes single-host (or
  a single sandbox per call); doesn't ladder
  one machine to another the way FITT does.
- **Capability-gap log** (`capability_gaps.jsonl`).
  Hermes has rich event logging but no
  dedicated "I'd need a tool to X" feed
  from agent failures.
- **HMAC-chained audit log**. Tamper-evident
  forensic trail. Hermes has detailed
  trajectory and event logs but not a
  chained one.
- **Phase 4.8 growing-bubble Telegram
  renderer** — Hermes's Telegram renderer
  is closer to "post one reply when
  done." FITT's is sharper for long
  responses and tool sequences.

#### Summary

The audit's takeaway: **Hermes is OpenClaw
in Python**, with extra emphasis on the
self-improving loop (curator + skill_usage
sidecar + LLM-judged consolidation) and the
serverless-backend story (Modal, Daytona,
Vercel Sandbox). For FITT-side problems:
- The skills-loader recommendation from the
  OpenClaw audit is reinforced — two
  reference systems converged on
  markdown-skills as the substrate. Cement
  it as the highest-leverage change.
- Honcho-as-memory-provider is the most
  important Phase 7 (memory v1) discovery
  — a real cross-session user-modeling
  service exists, with an MIT-licensed
  Python SDK and a clean plugin pattern.
- The `[SILENT]` cron response convention
  is cheap and good. Worth borrowing
  immediately — few hours of work.
- Curator's pure-transitions + LLM-judged-
  consolidation + recoverable-archive
  pattern is the reference for any future
  "stale lessons" sweep.
- FTS5 + anchored-window session search is
  the reference for cross-session search
  when Phase 7 lands.
- Provider-level timeout config keys (vs
  one global) is the right schema for
  Phase 5+ config split.

Borrow-list updates (added at the bottom of
the OpenClaw pick-list below):
- `[SILENT]` cron response convention (~hours)
- Honcho memory plugin evaluation (1-2 days)
- FTS5 anchored-window session search
  (1-2 days, when Phase 7 lands)
- Provider-level timeout config keys
  (~half day)
- Per-click Telegram approval-button user
  authorization (~hours)

Don't borrow: the curator (too much
machinery for FITT's current pile), the
serverless-terminal backends (off Principle
5), the multi-key auth rotation (not
needed at single-user scale), the embedded-
PTY dashboard (Windows incompatibility on
the Hub).

*Status:* [NousResearch/hermes-agent on
GitHub](https://github.com/NousResearch/hermes-agent).
Active development through May 2026. MIT
licensed. Built by Nous Research; closely
coupled to their Nous Portal model service
but not exclusive to it.

### Beever Atlas (Votee AI + Beever AI)

*What:* Memory layer. Turns chat across Telegram,
Discord, Mattermost, Teams, Slack into a Neo4j knowledge
graph + auto-generated wiki + MCP-ready memory layer. Not
a full agent — just the memory substrate.
*Relevance to FITT:* directly slots into Phase 7 "Memory
v1 (RAG, compaction, cross-project)." Specifically the
cross-surface memory part: FITT's memory today is
markdown-per-session; Beever is "structured memory from
all your chat surfaces."
*Why not adopt (yet):* too early. FITT doesn't have
enough multi-surface data yet to benefit from a
knowledge-graph layer. Revisit when Phase 3.5 (Open
WebUI) ships and we have more than Telegram.
*Watch for:* how "MCP-ready memory layer" is actually
exposed. If it's an MCP server we can point FITT at when
the time comes, that's a clean adoption path.
*Status:* Open-sourced October 2026 per press release.

### Claude Code / Aider / Kiro / Cursor / Continue

*What:* Coding agents. Kiro is Amazon's, Cursor is a
commercial IDE, Claude Code is Anthropic's terminal-first
tool, Aider is open-source terminal-first, Continue is
open-source IDE-plugin-first.
*Relevance to FITT:* these are the "coding CLI" layer
in the MeshClaw pattern. If FITT ever wraps a coding
agent rather than building one, these are the
candidates.
*Why not adopt (yet):* deferred. Current stance: use
these directly for IDE coding work (route their model
requests through FITT), don't try to wrap them into
FITT's agent loop. See earlier architecture
conversations.
*What to borrow:* per-turn visibility patterns (already
noted in hallucinations-and-poisoning.md as Problem D).
Context compaction strategies (already noted in same
doc). Skills-system precedence (noted above under
OpenClaw).

## Routing/gateway projects (alternatives to LiteLLM)

We're on LiteLLM. Principle 3 says don't reinvent what
works. A scan of alternatives as of May 2026 in case
LiteLLM ever stops being the right fit:

- **Bifrost** — Go-based, claimed fastest open-source
  AI gateway. Newer. Worth benchmarking if LiteLLM
  becomes a bottleneck (it isn't).
- **Kong AI Gateway** — enterprise shape, heavy for
  single-user. Pass.
- **Portkey** — SaaS-first with OSS components.
  Orientation is wrong for local-first.
- **Cloudflare AI Gateway** — edge-based. Wrong
  topology for two-machine Tailscale cluster.
- **OpenRouter** — not a self-hosted gateway; it's a
  paid routing service. We use it as a backend *via*
  LiteLLM.
- **Ollama** — model server, not a gateway. Already in
  the stack as a backend.
- **vLLM / TGI / llama.cpp server** — inference servers.
  Can slot in as an alternative to Ollama for the
  Compute node. Worth evaluating if local inference
  throughput ever matters.

Bottom line: LiteLLM remains the right choice for FITT's
scale and topology. Revisit only if we hit a concrete
LiteLLM limitation.

## External services (for tools that need them)

Not frameworks — data sources FITT tools might call. Listed
here so the evaluation survives past whichever chat
conversation surfaced the need.

### Web search

Triggered by the Phase 7+ `web_search` tool. The viable
options under FITT's no-subscription constraint
(Principle 5):

- **SearXNG** — self-hosted meta-search. Docker container,
  aggregates results from Google/Bing/DuckDuckGo/Brave/
  others without upstream keys. MIT licensed. No query
  logging leaves your machine. Adds one compose service.
  The right long-term choice; matches FITT's local-first
  principles cleanly.
- **DuckDuckGo Instant Answers API** — free, no key,
  `https://api.duckduckgo.com/?q=...&format=json`. Returns
  structured "instant answers" for a subset of queries
  (Wikipedia abstracts, conversions, disambiguation). Not
  general search. Good as `web_search` v0 because it
  requires zero infrastructure — just an HTTPS call like
  `http_get`. Upgrade to SearXNG when the gaps show.
- **Brave Search API** — free tier 2000 queries/month with
  an API key. Good quality. Technically not a subscription
  but closer to the line; keys can be revoked, quotas
  apply. Reasonable fallback if SearXNG proves too heavy
  operationally.
- **Google / Bing APIs** — require billing; out of scope
  per Principle 5.
- **Scraping Google / Bing HTML directly** — violates ToS,
  unreliable, and the model's own output layer knows this.
  Don't.

### Other current-facts APIs

The "find me X" pattern applies across domains; each has
open endpoints that `http_get` can already hit today if
the model thinks to try them.

- **Weather** — `wttr.in` (already used live 2026-05-10).
  No key, ASCII or JSON.
- **Stocks / crypto** — `api.coingecko.com` for crypto;
  Yahoo Finance JSON endpoints for stocks. Both no-key.
- **GitHub** — `api.github.com`, 60 req/hour unauth, 5000
  req/hour with a user token.
- **Hacker News** — `hacker-news.firebaseio.com/v0/`. No
  key, very clean API.
- **Public holidays** — `date.nager.at` per-country
  endpoint.
- **NASA APOD / space imagery** — `api.nasa.gov`, generous
  free tier.
- **Sports** — TheSportsDB has a free tier. Per-league
  endpoints exist; evaluate per-need.

For most of these, the right FITT pattern isn't new tools
— it's the Phase 7+ current-facts nudge in the capability
block teaching the model to reach for `http_get` against
known-good URLs when asked about current X. A `web_search`
tool is the catch-all for anything without a direct API.

## Opportunities pick-list (2026-05-15)

Closing summary from the OpenClaw audit. Sized for the
"opportunistic upgrades" mode FITT is now in. Each row
ties to a specific question we've hit during FITT
development, with rough effort. **Treat as a pick-list,
not a backlog.** Pull one when an evening goes that
way.

| Q answered                                       | Item                                                | Effort         | Trigger condition |
|--------------------------------------------------|-----------------------------------------------------|----------------|-------------------|
| "How do I add web search / gmail / gh without writing a tool every time?" | Skills-as-markdown loader                           | Half day       | Next "FITT can't do X" complaint that maps to a CLI |
| "Where does FITT-can't-do-X go in my UX?"        | Default web search (markdown skill on `http_get` + DuckDuckGo) | Half day after loader exists | Anytime you wish FITT could "just look that up" |
| "Cron fires too often; how do I keep things quiet?" | Heartbeat structured-outcome schema (`{outcome, notify, priority}`) | 1-2 days       | "I want FITT to wake every 30m and check X" with a real X |
| "How do I tell the user the right thing on timeout/error?" | Better-shaped operator error messages (name the config key, explain layering) | Few hours, opportunistic | Whenever an existing error path comes up; `upstream_silent` is the next candidate |
| "If I extend approval callback data, what's the limit?" | Telegram callback-data alias rewrite (note for the future) | Note only      | If we ever encode > id+decision in the callback |
| "Could the agent walk me through setting up <thing>?" | Agent-first setup recipes (markdown drops the agent reads) | Half day each  | When we have a concrete X worth setup-recipe content |
| "What about hallucinated tool calls?"            | Before-tool-call validation hook                    | 1-2 days       | If hallucinated-tool-call rate becomes operationally annoying |
| "If we ever ship sub-agents, how do we sandbox?" | Per-agent Docker sandbox                            | 3-5 days       | When sub-agents ship — not before |
| "Operator-side dashboard?"                       | Minimal web UI over Phase 4.8c HTTP endpoints       | 2-3 days       | When the operator-grep-the-logs tax becomes annoying |
| "Multiple OAuth keys per provider with rotation?" | Auth-profile rotation                              | n/a — LiteLLM territory | Single-user FITT doesn't need this |
| "Voice / RAG / vector memory?"                   | Phase 7-8 territory                                | Real phases    | Per the roadmap |
| "Per-turn 'think harder' without alias swap?"    | Surface `thinking` / `reasoning` level in chat request | Half day       | If a future model with adaptive thinking is on `fitt-smart` and we want per-turn control |
| "Cheaper model for compaction / TTS / hooks?"    | Task-specific model overrides                      | Half day each  | Lands with Phase 7 (compaction) and Phase 8 (voice) — pattern worth borrowing then |
| "Notification fatigue from cron jobs that fire even when nothing changed?" | `[SILENT]` cron response convention (Hermes audit, 2026-05-21) | Few hours | Anytime a cron firing pings even though there was nothing to say |
| "Cross-session user modeling for the Phase 7 memory v1 question?" | Honcho memory plugin evaluation (Hermes audit) | 1-2 days for v0 | Phase 7 kickoff |
| "How do I find what we discussed three weeks ago?" | FTS5 + anchored-window session search (Hermes audit) | 1-2 days | When Phase 7 lands and the session DB exists |
| "Long-running provider (local Ollama) vs short-running (cloud) timeouts?" | Provider-level timeout config keys (Hermes audit) | Half day | When `upstream_silent` shape needs per-provider tuning — observed in NVIDIA queue-depth incidents |
| "If a non-operator is added to the Telegram chat, can they approve commands?" | Per-click approval-button user authorization (Hermes audit) | Few hours | Before a second person joins the operator chat |

The single highest-leverage change is the **skills
loader**. It's small (half day), it answers a class of
"how do I add X" questions cheaply, and it makes
~20 of OpenClaw's bundled skills usable in FITT
without modification.

If the loader gets built, the OpenClaw `skills/`
directory becomes a content reservoir worth scanning
periodically. Per-skill review is short — most are
~50 lines of markdown — and each is independently
adoptable. As OpenClaw adds more skills upstream,
FITT can pull selectively.

## When to revisit this doc

- A new project in the same space surfaces that looks
  substantially different from what's already listed.
- We're about to start a phase (7+ memory, 8 voice, 9
  home-assistant) and a reference system on this list
  has specific patterns worth cribbing.
- An existing entry goes stale — project abandoned,
  relicense, fundamental architecture shift.
- We find ourselves about to build a thing and haven't
  checked whether someone already did it.

Add entries at the bottom of the relevant section with
a one-paragraph scan + explicit "why not adopt" so we
don't have the same conversation three times.

## Not on this list

Out-of-scope for the doc but worth noting so we know
what we're not tracking:

- Pure LLM inference engines (vLLM, TGI, llama.cpp).
  Touched only as potential backends; not agent
  frameworks.
- Commercial SaaS agents (ChatGPT, Claude.ai, Gemini).
  Useful for comparison but can't be adopted.
- Browser-based agents (AutoGPT-style, Manus). Different
  shape — they're about autonomous browsing, FITT is
  about persistent messaging.
- Home-automation platforms (Home Assistant itself).
  Listed in the roadmap as a Phase 9 *integration*, not
  a FITT replacement.

If one of these stops being out-of-scope, promote it to
a section above with a proper evaluation.
