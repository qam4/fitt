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

Repository: `qam4/home-ai-cluster` (private).

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
   Ollama + OpenRouter) - IN AT-HOME VERIFICATION
2. Memory v0 (identity + today's conversation log as markdown)
3. Telegram + Open WebUI (phone/browser interfaces)
4. Agentic tools (via MCP, approval-gated)
5. Retro-AI integration (launch/monitor RL training via FITT)
6. Autonomy (cron + heartbeat)
7. Memory v1 (RAG, compaction, cross-project)
8. Voice (Faster-Whisper STT + Piper/Kokoro TTS) - optional
9. Home Assistant agent ("Alexa-like" half of the vision)
10. Hardening - ongoing

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
