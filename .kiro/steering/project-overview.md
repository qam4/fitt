---
inclusion: always
description: FITT overview - what it is, the two-machine split, guiding principles
---

# FITT - Project Overview

**FITT** (Fred Industries Two Thousand) is a self-hosted, spec-driven
personal AI assistant with persistent memory, agentic tools, and
multi-interface reach. It runs on a two-machine GPU cluster
(iBUYPOWER desktop "Hub" + Acer Predator laptop "Compute") connected
by Tailscale.

Repository: `qam4/fitt` (private).

## Scope: what FITT is and is not

**FITT is a personal AI assistant** in the MeshClaw / OpenClaw
shape. It excels at:

- Cron + proactive notifications ("monitor X, ping me when Y").
- Read-mostly project queries ("status of training run Z",
  "find me last week's spec").
- Memory across sessions, learn-corrections, identity.
- Routing hard turns to cloud, easy turns to local.
- Multi-interface reach: phone (Telegram), browser (Open WebUI),
  voice (Phase 8), Home Assistant (Phase 9).
- Tool surface for short / side-effect-light tasks: send_message,
  http_get, list_directory, grep, status checks, cron management,
  learn_*.

**FITT is not a coding agent.** Decision codified
2026-05-13. The user uses dedicated coding tools (OpenCode,
Cursor, Kiro, Claude Code) for editing code in an IDE with
diff review. FITT can READ code (read_file, grep_repo,
list_directory) but should not be the primary venue for
write-side editing tools (write_file, edit_file,
project_shell-with-mutation) — particularly not from
Telegram, where the small-screen no-diff-review UX is a
footgun. Future work should not invest in making FITT
better at code editing; if a session is shaped like
"FITT, edit this file for me", the correct response is
to point at the IDE.

This rules out:

- Phase 6 ("FITT writes code from a spec") as originally
  framed. If revisited, it should be reshaped to "FITT
  hands tasks to OpenCode and monitors", not "FITT
  writes the code itself."
- Optimisations targeted at making the file-edit /
  code-edit experience smoother in routine chat
  (multi-block edits, line-range insertion, patch
  formats, etc.). They are not on the critical path.
- Renderer / approval-UX work specifically aimed at
  code-edit task cards. The renderer should be good
  enough for any tool-using turn (cron status, http_get,
  send_message, grep results); we don't agonise over
  edit_file failure visualisation.

This rules in:

- Memory v1 (RAG, cross-project recall) — the
  assistant-shape feature, high payoff.
- Voice (Phase 8).
- Home Assistant integration (Phase 9) — biggest
  potential daily-life payoff.
- Cron / proactive-monitoring polish.
- Hardening of what we have (Phase 10).

## Inspiration sources

Decided 2026-05-15 after a side-by-side install of
OpenClaw on the same NAS that runs FITT. The author
ran OpenClaw, saw what it does well, and chose to
keep building FITT rather than migrate. The
direction:

- **MeshClaw / OpenClaw** are the inspiration source
  for the personal-AI-assistant shape: skills,
  channels, multi-interface, "talk to it and it
  walks you through setup," default-on web search,
  Google Workspace integration via CLI tools like
  `gog`. When FITT considers a feature in that
  shape, look first at how OpenClaw does it; their
  skills directory is MIT-licensed markdown and
  often portable as-is.
- **OpenCode, Cursor, Kiro, Claude Code** are the
  inspiration source for code-edit / spec-driven
  workflows. FITT is explicitly NOT a coding agent
  (see above) but borrows discipline patterns from
  these tools where relevant (spec-first feature
  design, structured tool errors, approval-gated
  mutations).

FITT is not trying to be either. It's a single-user
learning project where architecture and use cases
come from the author's own needs. Inspiration is
welcome; replication for completeness's sake is not.

## Items observed in OpenClaw worth borrowing opportunistically

Captured during the 2026-05-15 OpenClaw evaluation
so future-author doesn't have to re-derive them:

- **Better timeout error messages.** OpenClaw's
  "LLM request timed out" message names the
  specific config key (`models.providers.<id>.
  timeoutSeconds`) and explains the layering
  between provider and agent timeouts. FITT's
  `upstream_silent` (Phase 4.9) is good; copying
  this style of operator-facing message would be
  better. ~1 hour of work.
- **Skills-as-markdown.** OpenClaw skills are
  `SKILL.md` files: frontmatter + markdown body
  describing a CLI the agent can shell out to. No
  code, no plugin contract. Half-day to add a FITT
  skills loader; opens the door to dropping in
  OpenClaw's gog / gh / jq / web-search markdown
  unchanged. The cleanest single architectural
  upgrade FITT could make.
- **Setup recipes the agent can drive.** "Help me
  set up X" works in OpenClaw because the docs are
  written FOR the agent: numbered steps, exact
  commands, fallback paths if something fails.
  Same substrate FITT already has (system prompt
  injection); just missing the content. ~half a
  day per recipe.
- **Default-on web search.** The single biggest
  day-1 UX delta versus FITT today. With a skills
  loader (above), this becomes a markdown drop
  pointing at `curl` + DuckDuckGo's HTML endpoint
  or similar. Without the loader, it's a real
  inline tool. Either is fine; the loader is more
  reusable.

These are opportunistic upgrades, not a phase plan.
Pick one when an evening goes that way; resist
treating the list as a backlog.

Key documents (read in this order when context needed):

- `README.md` - landing page
- `FITT_ROADMAP.md` - guiding principles, phase plan, inline drafts
  for phases 2-10
- `FITT_PRD.md` - original product requirements
- `docs/quickstart.md` - the one install doc, end-to-end
- `.kiro/specs/phase<N>-<name>/` - active spec when working on a
  phase, three files: `requirements.md`, `design.md`, `tasks.md`

## Two machines

| Role    | Machine            | Runs                                 |
|---------|--------------------|--------------------------------------|
| Hub     | Always-on desktop  | FITT gateway, fallback Ollama (small) |
| Compute | Laptop (bigger GPU) | Primary Ollama (bigger model)        |

Tailscale is the trust boundary. The gateway exposes HTTP on
`:8080`, bound to the Tailscale interface only (via Windows Defender
Firewall on the Hub).

## Phase plan (summary)

0. Bootstrap (Ollama + IDE + local LLM) - DONE
1. Gateway v0 (OpenAI-compatible HTTP daemon, alias routing,
   Ollama + OpenRouter) - DONE
2. Memory v0 (identity + today's conversation log as markdown) - DONE
3. Telegram + Open WebUI (phone/browser interfaces) - DONE
4. Agentic tools (via MCP, approval-gated) - DONE
4.5. Cron + proactive notifications - DONE
4.6. End-to-end test harness - DONE
4.7. project_shell tool - DONE
4.8. Visibility proxies (per-turn event stream, Telegram
     live-turn renderer, fitt watch CLI) - DONE
4.9. Upstream timeout discipline - DONE
4.10. Skills loader - DONE
4.11. Web search tool - DONE
5. Lessons + decaying history - CODE DONE, live validation pending
6. Spec-runner (RESHAPED — see roadmap; FITT not a coding agent)
7. Visibility & traceability - DONE 2026-05-28 (context
   discovery, /model + /lastturn + /status + /eval Telegram
   commands, Telegram markdown renderer, dashboard v0 with
   eval detail + edit substrate + typed actions)
8. Compaction (sessions full enough that this earns its keep)
9. Memory v1 (RAG, vector recall, cross-project)
10. Voice (Faster-Whisper STT + Piper/Kokoro TTS) - optional
11. Home Assistant agent ("Alexa-like" half of the vision)
12+. Opportunistic upgrades — single bullets, each landing when
     daily friction justifies it

## Guiding principles (from the roadmap)

1. Reach the inflection point fast.
2. Spec-driven from Phase 1 onward.
3. Use mature tools; don't reinvent. (LiteLLM, Ollama, Tailscale,
   FastAPI, uv, MCP.)
4. Each phase leaves something usable.
5. Cloud models for hard turns, local for the rest.
6. Security scales with risk.
7. Models are configuration, not architecture. Clients name
   logical aliases (`fitt-default`, `fitt-smart`), config binds
   aliases to concrete models.
8. The agent is honest about its capabilities. When a tool is
   missing, say what's missing and how to add it.
9. Live with it before extending it. Two weeks of real use between
   phases.
10. Shareable by construction. No personal values, secrets, or
    machine-specific paths in the repo. Users bring their own
    `~/.fitt/` directory.
11. Fail loud on detectable misconfigurations. Surface the error
    at boot or first request, never silently degrade. When the
    system can auto-detect the right answer (e.g. a client sending
    `X-FITT-Client`), use it — don't ask users to declare things
    the system can figure out.

## Model capability: the measurement ladder

A small but recurring slice (Principle 12: adapt the feature set to
the model you can run). Captured here so it isn't re-derived every
session. Two subjects — never conflate them:

- **Model** — "what can this model do?" A cost-tiered ladder.
- **Tools** — "are my tools well-formed / consistent (incl. MCP +
  skills)?" A cheap offline check that reads whatever's registered.
  *Not built yet.*

The model ladder, each rung strictly cheaper than the next:

- **ping / reachability** — is the host up? (no inference;
  `reachability.py`)
- **probe** — can it emit *one* tool call at all? (one inference;
  catches "reachable but narrates"; `alias_probe.py`)
- **eval** — how reliably does it tool-call across representative
  cases, under prompt pressure? (minutes, k-sampled;
  default / coding / realistic suites)
- **profile** — aggregate of declared facts + measured grades + cost
  vs a baseline (runs the eval engine + plan-election;
  `capability_profile.py`)
- **reconciler** — given the profile, which *enabled features* can
  this model drive? satisfied / unsatisfied / unknown
  (`capability_reconcile.py`)

"Benchmarking" is the informal umbrella for this whole activity, not
a specific rung. The rungs use *representatives*, not the full tool
inventory — the ladder tests the model, not every tool (so it doesn't
grow with the registry, and MCP/skills tools it can't foresee are the
tool-check's job, not the eval's). North star: **measure the model →
recommend features → operator confirms.** Never auto-drive off a
noisy, sample-limited measurement.

## Architecture highlights

- **Gateway** (Phase 1): FastAPI daemon on the Hub, runs as a
  Windows service via NSSM. OpenAI-compatible
  `POST /v1/chat/completions`. Bearer-token auth. Routes via
  LiteLLM to OpenRouter or Ollama based on alias.
- **Memory** (Phase 2+): markdown-first, three layers - identity
  (always injected), project context (swap by cwd), daily history
  (time-decayed).
- **Tools** (Phase 4): inline tools for file / git / shell /
  HTTP operations, plus optional MCP servers. A **project
  registry** at `$FITT_HOME/projects.yaml` names logical
  workspaces; the **SSH execution backend** dispatches each
  tool's shell command either locally or over `ssh <host> 'cd
  <path> && <cmd>'`. An **approval middleware** runs a deny
  list, per-session trust, per-client config overrides, and
  finally the tool's default bucket; `ask` prompts flow out
  over the Telegram poller to an inline keyboard. An
  **HMAC-chained audit log** records every tool call (including
  rejected and errored ones) at `$FITT_HOME/audit.jsonl`. A
  **capability-gap log** tracks "I'd need a tool to X"
  complaints for the natural next-tool backlog. See
  `gateway/README.md` for the operator-facing tour.
- **Interfaces**: IDE (Continue, Cursor, Kiro) via the
  OpenAI-compatible endpoint. Later: Telegram, Open WebUI, voice.

## Deployment neutrality

The gateway, telegram-bot, and open-webui are all **deployment-
neutral**: the code must run identically as a plain Python process
(native Linux, native Windows, or `uv run fitt serve` in a dev
loop) and as a Docker container. This is a code-level rule, not a
deployment choice — the current `docker compose` layout is the
recommended setup for a NAS hub, but not the only one.

Concretely:

- No `if running_in_container()` branches. Paths flow from
  `FITT_HOME` (env var, defaulting to `~/.fitt`) and resolve
  identically under both modes.
- SSH identity lives at `$FITT_HOME/ssh/id_ed25519` whether the
  container bind-mounts that directory or it sits on the host's
  native filesystem.
- Docker-specific glue (compose file, DNS for Tailscale MagicDNS,
  bind mounts) lives in the compose file and `.env`, not in the
  Python code.
- A future native-install doc will sit alongside today's
  Docker-focused `docs/quickstart.md`. Until that lands, users who
  want a native install follow the pre-Phase-3.5 instructions
  (Windows service via `scripts/install-service.ps1`) — today's
  gateway code already supports both paths; only the docs are
  Docker-first.

If a change forces a `if container: ... else: ...` branch or
embeds a path like `/fitt/...` directly in gateway code, that's a
smell; prefer surfacing a new env var / config option that both
deployments can set.

## Spec convention

Every phase from Phase 1 onward has a three-file spec under
`.kiro/specs/phase<N>-<name>/`:

- `requirements.md` - user stories with numbered acceptance
  criteria.
- `design.md` - architecture, modules, design decisions with
  rationale, correctness properties, testing strategy.
- `tasks.md` - checkboxed implementation tasks in sub-phases.

Inline drafts in `FITT_ROADMAP.md` get promoted to proper three-file
specs when a phase actually starts.
