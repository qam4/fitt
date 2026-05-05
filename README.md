# home-ai-cluster

> **FITT** - *Fred Industries Two Thousand.* A self-hosted, spec-driven personal AI with persistent memory, agentic tools, and multi-interface reach. Built gradually on a two-machine GPU cluster, demoable at every phase.

## What this is

A private project to build an always-on personal AI assistant that:

- Lives on my own hardware (desktop + laptop), reachable from anywhere via Tailscale.
- Routes model requests between local Ollama models and cloud models (OpenRouter by default) for hard turns.
- Persists memory across sessions so it acts like a partner, not a goldfish.
- Exposes agentic tools via MCP so it can *do* things, not just talk.
- Is reachable from my phone (Telegram), my IDE (VS Code + Continue / Cursor / Kiro), and eventually my watch (voice).

## Status

Phase 0 complete, Phase 1 in at-home verification.

## Docs

- **Install it:** [`docs/quickstart.md`](./docs/quickstart.md) - one page, ~30 minutes, start to finish.
- **How it works:** [`gateway/README.md`](./gateway/README.md) - config reference, HTTP API, CLI, troubleshooting.
- **Why it's built this way:** [`FITT_ROADMAP.md`](./FITT_ROADMAP.md) - guiding principles and phased plan.
- **Design FAQ:** [`docs/faq.md`](./docs/faq.md) - rationale for the non-obvious design choices. Read when you catch yourself asking "why not just X?".
- **Original vision:** [`FITT_PRD.md`](./FITT_PRD.md) - the product requirements document that seeded the design.
- **Active spec:** [`.kiro/specs/phase1-gateway/`](./.kiro/specs/phase1-gateway/) - three-file Kiro spec (requirements / design / tasks).

## Design commitments

- **No personal values in the repo.** Machine-specific IPs, tokens, and API keys live in `~/.fitt/` on each user's machine. The repo only contains `.example` templates.
- **Models are configuration, not architecture.** Swap Qwen for something newer by editing one line. Add a new cloud provider with a config entry.
- **Every phase is demoable.** Each milestone leaves a usable artifact, not foundational work for later phases.

## Convention

Specs follow the three-file Kiro convention:

- `requirements.md` - user stories with numbered acceptance criteria.
- `design.md` - architecture, modules, design decisions with rationale, correctness properties, testing strategy.
- `tasks.md` - checkboxed implementation tasks, organized into sub-phases.

When a phase's inline draft in `FITT_ROADMAP.md` is ready to implement, it gets promoted into a proper three-file spec under `.kiro/specs/<phase>/`.
