# home-ai-cluster

> **FITT** — *Fred Industries Two Thousand.* A self-hosted, spec-driven personal AI with persistent memory, agentic tools, and multi-interface reach. Built gradually on a two-machine GPU cluster, demoable at every phase.

## What this is

A private project to build an always-on personal AI assistant that:

- Lives on my own hardware (desktop + laptop), reachable from anywhere via Tailscale.
- Routes model requests between local Ollama models and cloud models (OpenRouter by default) for hard turns.
- Persists memory across sessions so it acts like a partner, not a goldfish.
- Exposes agentic tools via MCP so it can *do* things, not just talk.
- Is reachable from my phone (Telegram), my IDE (VS Code + Continue / Cursor / Kiro), and eventually my watch (voice).

## Status

Early scaffolding. Currently at Phase 0 complete, Phase 1 spec'd.

## Where to start

**Reading the design:**

- [`FITT_ROADMAP.md`](./FITT_ROADMAP.md) — the outer shell. Guiding principles, phase sequencing, and draft specs for later phases.
- [`FITT_PRD.md`](./FITT_PRD.md) — the original PRD that seeded the design.
- [`.kiro/specs/phase1-gateway/`](./.kiro/specs/phase1-gateway/) — the first full Kiro-style spec (`requirements.md`, `design.md`, `tasks.md`). This is what gets implemented next.

**Getting ready to run it:**

- [`docs/accounts-setup.md`](./docs/accounts-setup.md) — external accounts you need (OpenRouter, Tailscale, etc.) and what to collect before starting.
- [`configs/config.example.yaml`](./configs/config.example.yaml) — copy to `~/.fitt/config.yaml` and edit.
- [`configs/secrets.example.yaml`](./configs/secrets.example.yaml) — copy to `~/.fitt/secrets.yaml`, fill in keys, restrict permissions.

## Design commitments

- **No personal values in the repo.** Machine-specific IPs, tokens, and API keys live in `~/.fitt/` on each user's machine. The repo only contains `.example` templates.
- **Models are configuration, not architecture.** Swap Qwen for something newer by editing one line. Add a new cloud provider with a config entry.
- **Every phase is demoable.** Each milestone leaves a usable artifact, not foundational work for later phases.

## Convention

Specs follow the three-file Kiro convention:

- `requirements.md` — user stories with numbered acceptance criteria.
- `design.md` — architecture, modules, design decisions with rationale, correctness properties, testing strategy.
- `tasks.md` — checkboxed implementation tasks, organized into sub-phases.

When a phase's inline draft in `FITT_ROADMAP.md` is ready to implement, it gets promoted into a proper three-file spec under `.kiro/specs/<phase>/`.
