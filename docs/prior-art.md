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

### OpenClaw (Peter Steinberger, now OpenAI-sponsored)

*What:* Self-hosted personal AI assistant in
TypeScript. Local gateway + 20+ messaging channels
(WhatsApp, Telegram, Slack, Discord, Signal, iMessage,
Matrix, etc.) + skills system + workspace. MIT license.
Creator joined OpenAI Feb 2026; project moved to an
independent foundation with OpenAI as financial sponsor.
*Relevance to FITT:* uncomfortably close pattern match.
Same local-first, markdown-driven-config, per-session
memory, skills-as-markdown-dirs shape we converged on
independently. Six to twelve months further along on most
surfaces.
*Why not adopt:* TypeScript base; sprawling scope (20+
channels, voice, Canvas, multi-agent routing) compared
to FITT's narrow Telegram + gateway + memory. Opinions
not all ones we share — OpenClaw is reactive (DMs in,
agent out) where KITT's vision leans proactive
(scheduled + heartbeat + cross-surface persistence).
Switching means inheriting a much bigger codebase than
one person can comfortably maintain.
*What to borrow:*
- **Skills system** — `SKILL.md` files in
  `workspace/skills/<name>/`, three-tier precedence
  (bundled < global < workspace). Cleaner than FITT's
  current ad-hoc `identity/` approach once we want more
  than three files.
- **DM pairing allowlist** — unknown senders get a
  pairing code; bot doesn't process their messages until
  approved. Better than today's "bearer token + client
  tag" model for future multi-channel expansion.
- **Sandbox-for-non-main-sessions** — Docker by default,
  SSH + OpenShell backends. Lines up with Phase 7+ "OS-
  level agent sandbox" item already on the roadmap.
- **Agent prompt file names** — `AGENTS.md`, `SOUL.md`,
  `TOOLS.md` is a cleaner naming convention than FITT's
  `user.md / soul.md / tools.md`. Low-stakes to adopt.
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
