# Retro-KITT: Product Requirements Document

> A self-hosted, voice-capable, memory-persistent personal AI assistant built on a home GPU cluster. Accessible from phone, watch, and IDE — with a single shared brain.

---

## 1. Vision & Value Proposition

### 1.1 North Star
A personal AI that behaves like KITT from Knight Rider: always available, always remembers, speaks naturally, and takes action. It lives on the user's hardware, knows their code, and can be addressed from any device the user already owns.

### 1.2 Core Value Props
- **Zero recurring cost** — no token limits, no monthly fees after hardware is owned.
- **Full privacy** — code, conversations, and voice never leave the home network.
- **Persistent memory** — context survives across sessions, devices, and projects.
- **Multi-interface** — chat (Telegram), voice (watch), IDE (Cursor/Kiro/VS Code), CLI (Aider) all share the same brain.
- **Agentic execution** — the assistant can read, write, and run code on behalf of the user.

### 1.3 Non-Goals (v1)
- Not a replacement for frontier cloud models on novel research tasks.
- Not a multi-user product. Single-user, single-household.
- No cloud hosting, no SaaS billing surface.

---

## 2. User Personas

### 2.1 Primary: The Engineer-Owner (you)
- Software engineer, familiar with Docker, Python, Tailscale, git.
- Owns two rigs: always-on desktop + high-VRAM laptop.
- Works on personal GitHub projects (e.g., `retro-ai`) across contexts: desk, couch, travel.
- Currently uses Claude Sonnet and hits monthly limits.

### 2.2 Future: The Technical Enthusiast (productization)
- Comfortable with a guided install but not writing Docker from scratch.
- Wants the KITT experience without designing the architecture.
- Target of v2 "one-command installer."

---

## 3. Hardware Baseline

| Role | Machine | GPU | VRAM | RAM | Purpose |
|------|---------|-----|------|-----|---------|
| Hub (always-on) | iBUYPOWER Trace 5 MR | RTX 3070 | 8 GB | 16 GB | Gateway, memory, orchestration, small-model fallback |
| Compute (on-demand) | Acer Predator Helios Neo 16S | RTX 5070 Ti | 12 GB | 32 GB | Large-model inference (14B/32B), RL training |
| Interfaces | Phone, smartwatch, laptop IDE | — | — | — | User-facing clients |
| Transport | Tailscale mesh | — | — | — | Encrypted inter-device network |

---

## 4. System Architecture

### 4.1 Layered View

```
┌─────────────────────────────────────────────────────────────┐
│  Interfaces: Watch voice, Telegram, Cursor/Kiro/VS Code,    │
│              Open WebUI, Aider CLI                          │
└──────────────────────────┬──────────────────────────────────┘
                           │ (OpenAI-compatible API)
┌──────────────────────────▼──────────────────────────────────┐
│  Gateway Layer (on Hub)                                     │
│  ├─ LiteLLM proxy (routing, failover, caching)              │
│  ├─ Agent runtime (LangGraph / Autogen / custom)            │
│  └─ Tool registry (file R/W, exec, git, train, notify)      │
└──────────────┬──────────────────────────────┬───────────────┘
               │                              │
┌──────────────▼───────────┐      ┌───────────▼──────────────┐
│  Memory Layer (on Hub)   │      │  Inference Layer          │
│  ├─ Mem0 / Zep (episodic)│      │  ├─ Ollama @ Compute      │
│  ├─ Qdrant/Chroma (RAG)  │      │  │   (14B/32B Qwen)      │
│  ├─ Redis (session)      │      │  └─ Ollama @ Hub          │
│  └─ Postgres (facts)     │      │      (7B fallback)       │
└──────────────────────────┘      └──────────────────────────┘

          Voice Services (on Hub)
          ├─ Faster-Whisper (STT)
          └─ Piper / Kokoro (TTS)
```

### 4.2 Data Flow: Voice Request from Watch
1. User dictates voice note via Telegram on watch.
2. Hub's Telegram bot receives `.ogg` file.
3. STT service transcribes → text.
4. Gateway injects memory context (episodic + semantic RAG).
5. LiteLLM routes to Compute (if online) or Hub fallback.
6. LLM responds; if tool call, Gateway executes against sandboxed workspace.
7. Response is synthesized via TTS.
8. Bot replies with text + voice note.
9. Memory Layer extracts and persists new facts asynchronously.

---

## 5. Functional Requirements

### 5.1 Interfaces
- **FR-I1** Telegram bot accepts text and voice messages; replies with text, voice, and images.
- **FR-I2** IDE plugins (Continue/Cursor) point at Gateway and share session memory with Telegram.
- **FR-I3** Open WebUI provides a mobile-browser fallback chat interface.
- **FR-I4** Watch interface uses Telegram voice notes as the primary input path (no custom watch app in v1).

### 5.2 Memory
- **FR-M1** Store episodic facts extracted from every conversation ("user prefers lr=1e-4", "retro-ai run 42 plateaued at 150").
- **FR-M2** Index local GitHub repos into vector DB nightly; on-demand re-index via command.
- **FR-M3** Each request injects top-K relevant memories + RAG snippets before LLM call.
- **FR-M4** Memory must survive reboots and Docker restarts (persistent volumes).
- **FR-M5** Cross-interface consistency: a fact learned via Telegram is available in the IDE within seconds.

### 5.3 Agentic Capabilities
- **FR-A1** Tool: read/write files within whitelisted workspace directories.
- **FR-A2** Tool: execute whitelisted shell commands (`pytest`, `npm test`, `git status`, training scripts).
- **FR-A3** Tool: launch RL training on Compute node with parameter overrides.
- **FR-A4** Tool: generate plots from logs and deliver as images.
- **FR-A5** Every destructive action requires confirmation from the user via chat before execution.

### 5.4 Inference Routing
- **FR-R1** Default route: Compute node (5070 Ti) when reachable on Tailscale.
- **FR-R2** Failover: Hub node (3070) with smaller model when Compute is offline.
- **FR-R3** Optional third tier: OpenRouter/Claude for flagged "hard" requests.
- **FR-R4** Health check endpoint reports current active route.

### 5.5 Voice
- **FR-V1** STT: sub-2-second transcription for 15-second clips (local, CPU/GPU).
- **FR-V2** TTS: configurable voice persona (default: KITT-like).
- **FR-V3** Voice notes stored temporarily; deleted after processing unless flagged.

### 5.6 Notifications
- **FR-N1** Training jobs emit progress updates to Telegram every N episodes.
- **FR-N2** Job completion pushes summary + reward curve image to chat.
- **FR-N3** Failures push stack trace + suggested fix to chat.

---

## 6. Non-Functional Requirements

### 6.1 Performance
- **NFR-P1** First-token latency on Compute node: < 500 ms for 14B model.
- **NFR-P2** Memory retrieval: < 200 ms for top-5 facts.
- **NFR-P3** End-to-end voice round-trip (watch → KITT → watch): < 8 seconds for 50-word response.

### 6.2 Reliability
- **NFR-R1** Hub uptime target: 99% (excluding planned reboots and power loss).
- **NFR-R2** Auto-restart policy on all Docker services.
- **NFR-R3** Memory writes are durable before confirming to user.

### 6.3 Security
- **NFR-S1** All inter-device traffic rides Tailscale; no public ports exposed.
- **NFR-S2** Telegram bot enforces allowlist on `user_id`.
- **NFR-S3** Shell-exec tool runs in a constrained working directory; no `sudo`, no recursive deletes without double-confirm.
- **NFR-S4** Secrets live only in `.env`; `.env` is `.gitignore`'d.
- **NFR-S5** Ollama on Compute binds to `0.0.0.0` only on the Tailscale interface (not on the public Wi-Fi NIC).

### 6.4 Observability
- **NFR-O1** Structured logs for every LLM call (model, route, latency, token count).
- **NFR-O2** `/status` command returns active route, loaded models, memory size, uptime.
- **NFR-O3** Weekly log rotation.

---

## 7. Tech Stack (Current Decision Set)

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Inference | Ollama | Lightweight, background service, wide model support |
| Chat model | Qwen2.5-Coder 14B / 32B | Strongest open coding model that fits the rigs |
| Embed model | nomic-embed-text | Small, local, fast |
| Gateway | LiteLLM | OpenAI-compatible, handles routing + failover |
| Agent runtime | LangGraph (evaluate Autogen) | Stateful multi-step reasoning |
| Episodic memory | Mem0 (evaluate Zep) | Purpose-built for agent memory |
| Vector DB | Qdrant or ChromaDB | Local, embedded, no ops overhead |
| IDE | Kiro (specs) + Cursor (execution) | Complementary workflows |
| CLI agent | Aider | Multi-file edits, repo map, auto-commit |
| Messaging | Telegram + python-telegram-bot | Cross-platform, watch support, voice notes |
| STT | Faster-Whisper | Local, fast, high accuracy |
| TTS | Piper or Kokoro-82M | Local, natural voice |
| Remote desktop | RustDesk (self-hosted relay) | Open-source, Tailscale-friendly |
| Transport | Tailscale | Already configured |
| Orchestration | Docker + docker-compose | Reproducible, versionable |

---

## 8. Repository Structure (`home-ai-cluster`)

```
home-ai-cluster/
├── README.md                      # Entry point, quick start
├── PRD.md                         # This document
├── ARCHITECTURE.md                # Deep dive with diagrams
├── .gitignore                     # Secrets & bulky artifacts
├── .env.example                   # Template; real .env stays local
├── docker-compose.yaml            # Hub services
├── configs/
│   ├── litellm_config.yaml        # Routing rules
│   ├── ollama_models.txt          # Pull list
│   └── tailscale_acl.md           # Network policy notes
├── services/
│   ├── telegram-bot/              # Voice + text interface
│   ├── memory-service/            # Mem0/Zep wrapper
│   ├── stt-service/               # Faster-Whisper
│   └── tts-service/               # Piper/Kokoro
├── scripts/
│   ├── memory_update.py           # Nightly RAG re-index
│   ├── launch_training.py         # Bridge to Compute node
│   └── healthcheck.py             # Status probe
├── tools/                         # Agent tool implementations
│   ├── file_io.py
│   ├── shell_exec.py
│   ├── git_ops.py
│   └── plot_gen.py
└── docs/
    ├── setup-day1.md              # Post-vacation checklist
    ├── port-map.md                # Which service uses which port
    ├── voice-pipeline.md          # STT/TTS internals
    └── memory-model.md            # Episodic vs semantic vs working
```

---

## 9. Rollout Plan (Phased)

### Phase 0 — Prep (while still on vacation)
- [ ] Create `@BotFather` bot, save token.
- [ ] Get Telegram user ID from `@userinfobot`.
- [ ] Sign up for OpenRouter (optional cloud failover).
- [ ] Create private GitHub repo `home-ai-cluster`.

### Phase 1 — Nervous System
- [ ] Install Docker Desktop + NVIDIA Container Toolkit on Hub.
- [ ] Install native Ollama on Compute; set `OLLAMA_HOST=0.0.0.0`.
- [ ] Pull models: `qwen2.5-coder:14b` (Compute), `qwen2.5-coder:7b` (Hub), `nomic-embed-text` (both).
- [ ] Verify Tailscale reachability both directions.
- [ ] Stand up LiteLLM with routing rules; smoke-test failover.

### Phase 2 — Chat Interface
- [ ] Build Telegram bot service.
- [ ] Wire bot → LiteLLM; verify end-to-end text chat from phone.
- [ ] Add allowlist and logging.

### Phase 3 — Memory
- [ ] Deploy Mem0 (or Zep) service.
- [ ] Add fact-extraction middleware to bot and IDE pathways.
- [ ] Run `memory_update.py` against `retro-ai` repo; verify RAG retrieval.
- [ ] "Keys on the counter" test: assert fact survives restart.

### Phase 4 — Agentic Tools
- [ ] Implement file R/W, shell exec, git, plot tools with sandboxing.
- [ ] Add confirmation flow for destructive actions.
- [ ] Integrate Aider as a tool the agent can invoke.
- [ ] Dry-run: "review last training, tweak config, launch new run" end-to-end.

### Phase 5 — Voice
- [ ] Deploy Faster-Whisper STT service.
- [ ] Deploy Piper/Kokoro TTS service.
- [ ] Handle Telegram `.ogg` inbound and outbound.
- [ ] Measure round-trip latency on watch.

### Phase 6 — IDE Integration
- [ ] Point Cursor/Kiro/VS Code Continue extension at Gateway URL.
- [ ] Verify shared memory between Telegram and IDE sessions.
- [ ] Document the "context handshake" (which project am I on?).

### Phase 7 — Hardening
- [ ] Add RustDesk self-hosted relay for GUI fallback.
- [ ] Weekly backup of memory DB.
- [ ] Log rotation and monitoring.

### Phase 8 — Productization (stretch)
- [ ] One-command installer script.
- [ ] Configurable persona/voice.
- [ ] Public architecture writeup / blog post.
- [ ] Consider open-sourcing the scaffold.

---

## 10. Success Metrics

- **Usage**: > 80% of daily LLM interactions flow through Retro-KITT rather than cloud Claude.
- **Memory accuracy**: > 90% hit rate on "do you remember X" tests after 30 days of use.
- **Voice latency**: median watch round-trip < 8 s.
- **Uptime**: Hub services 99%+ over 30 days.
- **Cost**: < $5/mo in optional cloud API usage after setup.

---

## 11. Open Questions / Research Items

- Mem0 vs Zep vs custom Postgres+pgvector — benchmark on actual workload.
- LangGraph vs Autogen vs handwritten state machine — complexity tradeoff.
- Best Kokoro voice for "KITT" persona; consider fine-tuning on reference audio.
- Quantization sweet spot: q4 vs q6 vs q8 for 14B on 12 GB VRAM at 32k context.
- Should IDE plugin write to memory directly or route through Gateway only?
- How to handle multi-project context switching (heuristic? explicit command? workspace detection?).
- Backup/restore strategy for memory DB.
- Evaluation harness: how do we regression-test the assistant's memory and tool-use?

---

## 12. Risks

| Risk | Mitigation |
|------|-----------|
| Laptop VRAM exhaustion at 32k context | Start at 16k, profile, raise carefully |
| Memory DB corruption loses history | Nightly snapshot to NAS (Tailscale) |
| Shell-exec tool exploited by prompt injection | Strict allowlist; sandbox working dir; confirm destructive ops |
| Ollama exposed beyond Tailscale | Bind only to Tailscale interface; verify with `netstat` |
| Compute node offline during deep session | LiteLLM failover + notify user of degraded mode |
| Telegram outage blocks all access | Open WebUI as secondary interface |
| Cloud model costs creep back | Hard monthly cap on OpenRouter key |

---

## 13. Glossary

- **Hub** — iBUYPOWER desktop, always-on gateway.
- **Compute** — Predator laptop, high-VRAM inference and training.
- **Episodic memory** — facts extracted from conversation ("user said X on date Y").
- **Semantic memory (RAG)** — vectorized code/docs retrieved on demand.
- **Working memory** — current LLM context window.
- **Gateway** — LiteLLM + agent runtime, the single entry point for all interfaces.
- **KITT** — the persona/product name for the assistant.

---

*Document status: DRAFT v0.1 — drafted during vacation, to be validated and iterated on Day 1 back at the rigs.*
