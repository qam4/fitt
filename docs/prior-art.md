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

*What:* Self-hosted, model-agnostic AI assistant.
Runs on local machine or VPS. Terminal + messaging
interfaces. Self-improving: repeated tasks become reusable
skills.
*Relevance to FITT:* the closest explicit-competitor
shape. "Install on your server, give it your messaging
accounts, persistent personal agent" — verbatim FITT's
pitch. Self-improving skills are what our lessons system
is reaching for.
*Why not adopt (preliminary):* haven't yet dug into it
seriously. From a skim: their skills-synthesis approach
is interesting but their hosting story seems
single-machine (we're two-machine cluster). Need a
deeper read before making a call.
*What to do:* actual evaluation on the Phase 7 memory
work. If Hermes has genuinely solved RAG + compaction +
skill-synthesis for a personal agent, that's a lot of
phases of work we don't need to build.
*Status:* [hermes-agent.ai](https://hermes-agent.ai/),
[hermes-agent.org](https://hermes-agent.org/). Active
development 2026.

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
