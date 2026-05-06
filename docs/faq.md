# FITT design FAQ

Questions that lead to design decisions. Each entry starts with a
concern a builder or user might arrive at — "why is it like this,
why not the obvious alternative" — and walks to what FITT does
and why.

If you catch yourself re-litigating one of these, add to this file
rather than re-debating. When an answer lives more naturally in
another doc (design.md, roadmap, steering), cross-link from here
rather than duplicating.

Comparisons to other tools (Kiro, Cursor, Continue, Claude Code,
Aider, ...) are fair game — they're often where the "why not X?"
arrives from.

---

## Core architecture

### Q: Why a gateway at all? Can't each client just call OpenRouter or Ollama directly?

You could, but you'd redo the same four chores in every client:
hold API keys, handle fallback, track cost, log what happened. And
the moment you swap a model (monthly-ish, as new ones drop), you'd
update every client.

The gateway is one place where:

- All secrets (Anthropic, OpenRouter, Ollama) live.
- Alias → model → backend routing is decided.
- Cost is logged in one comparable shape across backends.
- Approval and audit for agent tools (Phase 4+) happen.
- Clients authenticate with a Bearer token they can get rotated.

Clients (IDE, Telegram, Open WebUI, CLI) speak OpenAI schema to
the gateway and don't know or care what runs underneath.

**Related:** `FITT_ROADMAP.md` Phase 1; `gateway/README.md`.

---

### Q: What's an alias? Why `fitt-default` instead of naming the model directly?

Because the right model for "everyday coding" last month is not
the right one this month. LLMs improve faster than clients do.

An alias is a **role**: `fitt-default`, `fitt-smart`, `fitt-fast`.
Clients ask for a role; the gateway's config binds roles to
concrete models. Swapping `fitt-smart` from Claude Sonnet 4.5 to
Opus 5 is a one-line edit in `config.yaml`; no client touches
anything.

Concretely, a client sends `model: fitt-default` in its request.
The gateway's router resolves it through:

```
alias           → model id       → backend + endpoint + real tag
fitt-default    → qwen-coder-big → ollama @ laptop : qwen2.5-coder:14b
```

If the primary backend is unreachable, the router falls back to
the model's configured `fallback` and sets an `X-FITT-Backend`
header on the response so you can see what actually served.

This is Principle 7 of the roadmap, and it's load-bearing. The
alternative — clients naming `claude-sonnet-4-5` directly — works
until the model name changes, then breaks everywhere at once.

**Tool comparison:** LiteLLM and most gateways support this
pattern. What's specific to FITT is that **every client path
enforces it** — there's no escape hatch where a power-user sets
`model: claude-opus-4-5` and bypasses the alias layer. One
indirection, always.

**Related:** `FITT_ROADMAP.md` principle 7 and Phase 1 design;
`configs/config.example.yaml`.

---

### Q: How does the gateway pick a model when I pass an alias? What about fallback?

Three-step resolution:

1. **Alias → model id.** `fitt-default` → `qwen-coder-big`.
2. **Model id → backend + endpoint.** `qwen-coder-big` is defined
   with `backend: ollama`, `endpoint: http://laptop:11434`,
   `model: qwen2.5-coder:14b`.
3. **Fire the request** via LiteLLM using that backend's OpenAI-
   compatible shape.

If step 3 fails because the backend is unreachable (timeout,
connection refused, 5xx from the provider), the router:

- Reads the failing model's `fallback` field (another model id
  in the same config).
- Retries with the fallback.
- Sets `X-FITT-Backend` on the final response to identify which
  concrete model served.
- Logs a `failover` event.

Upstream 429/529 (provider overloaded) are **not** auto-retried;
they surface to the client as 503 with `Retry-After` so the
client decides. Fallback handles "backend can't be reached,"
not "provider told us to slow down."

Fallback is one-level. No chains. If the fallback also fails,
503. Multi-level fallback is tracked as "nice to have," not
shipping.

**Related:** Phase 1 design, failure handling section.

---

### Q: Why a hub + satellite split? Why not run everything on one box?

Because GPUs and always-on are different problems, and one
machine rarely nails both.

- **Hub** — always on, low-power, hosts the gateway and
  long-running services (Telegram bot, Open WebUI). A NAS or a
  small desktop works. No GPU needed.
- **Satellite** — has the GPU, runs Ollama, hosts whichever
  models you care about. A gaming laptop, a workstation, a cloud
  dev box. Doesn't need to be online 24/7; the gateway retries/
  falls back when a satellite is asleep.

A machine can play both roles if it has the GPU *and* you're
willing to leave it on. For a NAS hub + laptop satellite, the
split is the natural fit for the hardware you already own.

Satellites also host **execution** (Phase 4+) — the machine where
a project's code actually lives. The same split lets FITT run
tools on the satellite over SSH without needing to copy code to
the hub.

**Tool comparison:** IDE-embedded tools (Kiro, Cursor, Continue,
Claude Code) always run where the code runs; they don't need a
hub. FITT's hub is the "always-on identity" that lets non-IDE
clients (phone, browser, cron) talk to the same brain as the IDE.

**Related:** `docs/quickstart.md` Part A (hub) and Part B
(satellites); steering `project-overview.md`.

---

## Concepts

### Q: What are the core FITT concepts I should know?

Short version:

- **Gateway** — HTTP daemon that all clients talk to. One per
  hub. Handles auth, routing, logging, tools, approval.
- **Alias** — a logical model role (`fitt-default`). Clients ask
  for aliases, not concrete models.
- **Satellite** — a machine that hosts models (Ollama) and/or
  project code. Zero or more per hub.
- **Project** — a registered workspace: name + filesystem path +
  optional SSH host. Tools dispatch against projects.
- **Session** — a conversation scope with its own memory. Default
  is `main`, shared across interfaces. Named sub-sessions exist
  for side projects.
- **Tool** — a named capability FITT exposes to the LLM
  (`read_file`, `run_tests`, ...). Curated by intent, not a
  shell.
- **Client** — one of `ide`, `telegram`, `webui`, `cli`. Each
  client's requests carry a tag; per-client policy decides tool
  trust.

Each one has its own FAQ entry below.

**Related:** steering `project-overview.md` for the deployment
topology; each concept's own FAQ entry for rationale.

---

### Q: What's a project? Why do I need to register one, when Kiro/Cursor just "picks up" the directory I'm in?

Because FITT isn't "in" a directory the way an IDE is. A request
can arrive from Telegram, from cron, from a spec runner — none of
which have a cwd to imply. The request has to say *which
workspace it's about*.

A project is the answer: `name + path + optional ssh_host`,
registered once with `fitt project add`. Tool calls like
`read_file`, `grep_repo`, `run_tests` take a `project` argument.
The gateway looks it up, routes to the hub (local subprocess) or
the satellite (via SSH), scopes path resolution to the project
root.

This also buys you:

- **Per-project `test_command`.** `run_tests` runs that exact
  command. Portable across projects without the LLM guessing.
- **Cross-machine uniformity.** "Project X lives on laptop"
  becomes `ssh_host: frede@laptop`; tools route there
  transparently.
- **Scope boundaries.** Path resolution rejects `..` escape.
  Tools can't wander out of the project tree.
- **Approval policy per project** (future). A project can carry
  "always ask for writes here" even if the per-client default is
  `auto`.

**Tool comparison:**

- Kiro / Cursor / Continue use the cwd or the opened workspace
  folder. Works fine when there's always a visible UI.
- Aider has a similar per-session notion: you run `aider` in a
  directory and that's the scope. No registry; scope re-derived
  per invocation.
- FITT's registry exists because the scope has to persist across
  requests arriving from heterogeneous clients.

**Related:** Phase 4 design, project registry section;
`FITT_ROADMAP.md` Phase 4 and Phase 6 (spec runner, which *walks*
a project's tasks.md).

---

### Q: What's a session? Is it the same as a conversation?

Close but not the same.

A **conversation** is what you see in your client (Telegram
thread, IDE chat pane). A **session** is FITT's name for *a
scope of memory*: identity + lessons + per-day history files, all
bound to a session id.

Default session is `main`. Any request from any interface without
an explicit session id goes to `main`. So a comment you made from
Telegram this morning is visible when you open your IDE tonight.
One shared brain across interfaces.

Named sub-sessions exist for side projects or experiments:
`fitt session new retroai-debug`. They have isolated daily
history but share identity and lessons.

The failure mode this avoids: per-interface sessions. If
"Telegram memory" and "IDE memory" were separate, you'd
constantly re-explain context moving between phone and laptop.
FITT picks shared-by-default; explicit-when-you-want-isolation.

**Tool comparison:**

- Most chat UIs (ChatGPT, Claude.ai, Slack bots) scope memory per
  thread or per-conversation. Good for distinct topics; bad for
  "me across all my interfaces."
- Aider binds memory to the project directory. Closer to FITT's
  shape but coupled to filesystem.
- Mem0 and similar "persistent memory" tools overlap with FITT's
  session idea but pair it with vector stores; FITT's v1 is
  plain markdown (see memory entries below).

**Related:** `FITT_ROADMAP.md` Phase 2.5; Phase 5 (lessons +
decaying history).

---

### Q: What tools does FITT expose, and why not just a general shell like `executeBash`?

Today (Phase 4 read path):

- `spec_list`, `spec_read`, `spec_next_task`, `spec_mark_task` —
  spec-aware navigation.
- `read_file`, `list_directory`, `grep_repo`, `glob_search` —
  read-side filesystem.
- `git_status`, `git_diff` — read-side git.
- `list_capabilities` — introspection for the LLM.

Planned (Phase 4 write path + beyond):

- `write_file`, `edit_file`, `git_commit` — write-side, `ask`
  bucket.
- `run_tests(project)` — runs the project's configured test
  command.
- `http_get(url)` — with host allow/deny list.
- MCP servers for anything outside the curated set (Slack, Jira,
  Postgres, Home Assistant, etc.).

Why not a general shell?

Kiro, Cursor, Continue, Claude Code, Aider all expose some
variant of `executeBash` / `run_terminal_cmd`. Strictly more
flexible. Those tools ship in contexts where a human watches each
turn and clicks approve. FITT runs from Telegram (no screen),
from cron (asleep), from a spec runner walking `tasks.md` (you're
at work). "Human approves each shell command" doesn't scale
there.

So FITT tools are:

- **Narrow by name.** Approval policy is "read auto, write ask"
  without parsing command strings.
- **Predictable.** A trust decision holds because the tool shape
  doesn't drift. `run_tests` runs `project.test_command`. Always.
- **Auditable.** Grep `tool_invoked=run_tests` and you see every
  test run, period.

**The tell:** ask *would I approve this command in advance, for
every project, forever?* Test commands: yes. Arbitrary shell: no.

A project-scoped `run_shell` is in the Phase 4 follow-up list to
re-evaluate after 2 weeks of use. If the curated set consistently
leaves you wanting, we promote. Default stays narrow.

**Related:** Phase 4 design; Phase 4 tasks "Follow-ups" for the
run_shell status.

---

## Memory

### Q: How does FITT remember things across sessions and restarts?

Three layers injected into every request, in order:

1. **Identity** — `~/.fitt/identity/{user,soul,tools}.md`. Who
   you are, what FITT's role is, what tools are available. Always
   loaded, never summarized.
2. **Lessons** — `~/.fitt/lessons.md`. Human-curated bullets:
   "always run `ruff format` before committing," "the 'db'
   directory in project X is the production DB, never touch."
   Injected as `[Learned corrections]`.
3. **History** — per-session daily markdown files in
   `~/.fitt/sessions/<id>/history/YYYY-MM-DD.md`. Today's file
   injected verbatim; older days indexed (see memory-
   summarization entry below).

Write-through: every completed turn appends `## HH:MM user` and
`## HH:MM assistant` blocks to today's file. No separate DB. You
can `cat` the file.

"Keys on the counter" test: tell FITT something, restart the
gateway, ask about it same day — right answer.

**Related:** `FITT_ROADMAP.md` Phase 2, Phase 5.

---

### Q: Does FITT summarize old messages when context gets long, like Kiro does?

No. FITT never LLM-summarizes conversation history. The failure
modes in agents that do:

- **Decisions become discussions.** "We decided X" becomes "We
  discussed X." Model reopens the debate.
- **Specificity evaporates.** `gateway/src/backend.py:187`
  becomes "the backend." Model hunts for the file again.
- **Errors become truths.** An early failed attempt becomes "X
  doesn't work" after the fix landed. Model refuses to retry.
- **Corrections get dropped.** "No, not Y" loses the "not."
  Model proposes Y again.
- **Opacity.** You can't say "preserve X, summarize Y." All or
  nothing.
- **Hallucination.** A summary is a model's *prediction of what
  happened*, not a log.

FITT's approach instead:

- Today's history: verbatim.
- Yesterday: first entry + count.
- Days 3-30: one-line marker per day (date + count).
- Day 30+: dropped from context, kept on disk.
- Total history budget capped (~6000 chars).

Dumber than summarization, and deliberately so. The bet:
**consistency beats cleverness**.

**Still open:** today's history can get big mid-session. Phase 7
vector memory is the planned answer.

**Related:** `FITT_ROADMAP.md` Phase 5; this decision is the
"why not Mem0 / vector DB in v1" rationale.

---

### Q: Why not a vector DB (Mem0, Chroma, pgvector) from day one?

Because every vector setup adds:

- A service to run and back up.
- A retrieval step before every request (latency + failure mode).
- An embedding model dependency (pick one; it's not neutral).
- A surprising-retrieval failure mode — the wrong chunk surfaces
  and biases the response in a way that's hard to debug.

FITT v1's markdown-first memory has none of those. It's
file-based, greppable, versionable, human-editable. It trades
"semantic recall of month-old memory" for "reliable recall of
recent memory."

Phase 7 adds vector memory as an *opportunistic upgrade* —
triggered by "I notice FITT consistently forgets things older
than a week in a way that bites." Not before.

**Related:** `FITT_ROADMAP.md` Phase 5 and Phase 7.

---

## Control and safety

### Q: How does FITT decide whether to run a tool call? Does it ask me every time?

Every tool has an approval **bucket** resolved per call:

- `auto` — runs without prompting.
- `ask` — approval required; prompt goes to the originating
  client (or Telegram as fallback).
- `trust_session` — ask once per tool per session, auto after.
- `yolo` — auto-approve everything; time-boxed (30 min Telegram/
  WebUI, 6 h IDE/CLI).
- `block` — hardcoded deny (rm -rf /, git push --force,
  curl|sh).

Resolution order per call: per-call override > per-client policy
> per-tool default. Every decision, *including `auto`*, lands in
the audit log with HMAC-chained integrity.

Default policies by client:

- **IDE** — writes `auto` (the IDE shows diffs natively);
  shell-like tools still `ask`.
- **Telegram** — reads `auto`; writes and shell-like tools `ask`.
- **Open WebUI** — reads scoped; writes `block`; no shell. Least
  trust.
- **CLI on the hub** — writes `ask`; shell-like tools `ask`.

**Related:** Phase 4 design, approval section.

---

### Q: Can I tell FITT "always allow commands matching `sleep *`" the way I would in tool X?

No — and this is a deliberate non-goal. Pattern-based command
allowlists are a shell-injection surface.

The bug: bash lets you chain. `sleep 1; git commit -am evil` also
matches `sleep *` if the check is prefix-matching. The approval
middleware sees only the syntax; the shell resolves the
semantics. The LLM (or an attacker) wins by making the syntax
look innocent.

Chainers are endless: `;`, `&&`, `||`, `|`, `&`, `$(...)`,
backticks, `eval`. Even a parser that rejects `;` still has to
handle `$(...)` inside the approved part.

FITT's primitives sidestep the whole class:

- **Named-tool approval.** "Run `git_commit` with these args,
  yes/no." The tool is the unit.
- **Session-scoped tool trust.** "Trust `git_commit` for this
  session." Scoped to the session, the tool, the session.

A `sleep 1; git commit` can't bypass either. If you trust
`git_commit`, the only way in is the named tool. If you only
trust `sleep`, a multi-command string isn't an invocation of
`sleep`.

If FITT ever ships `project_shell`, it'll use the same
primitives — never patterns.

**Related:** Phase 4 design, approval section.

---

### Q: What's the deny list and why is it hardcoded?

Some commands should never run, regardless of config, approval
bucket, or trust decisions. The deny list (`gateway/tools/
deny_list.py`) is that list, in code:

- `rm -rf /` and variants
- `git push --force` / `git push -f`
- `curl ... | sh` and variants
- A few other obviously-destructive patterns

It's hardcoded (not config-driven) so no `config.yaml` typo or
operator error can open the door. Adding a pattern requires a
code change with a test that shows it catches the destructive
form and doesn't over-match benign forms.

Deny list runs **before** approval bucket resolution. No amount
of `yolo` bypasses it.

**Related:** Phase 4 design, deny list section; tasks 12a-12d.

---

### Q: How do I trust fewer (or more) things for a specific interface?

Two mechanisms:

1. **Client tag** — tokens in `secrets.yaml` carry `client: ide |
   telegram | webui | cli`. Per-client default buckets apply.
2. **Tool policy override** — `config.yaml` under `tools:` can
   override a tool's bucket globally or per-client:

   ```yaml
   tools:
     run_tests:
       default: ask
       per_client:
         ide: auto
     write_file:
       per_client:
         webui: block
   ```

When a request comes in, auth middleware tags the request with
its client. Tool middleware resolves the bucket: per-call
override > per-client policy > per-tool default > system
default.

If you want a stricter Open WebUI (the interface most likely to
be exposed to non-you users on the tailnet), tighten it in
`config.yaml`. If you want a looser IDE on your own laptop,
loosen it there. Different tokens = different policies.

**Related:** Phase 4 design; steering `conventions.md` for policy
conventions.

---

## Interfaces

### Q: Why three interfaces (IDE, Telegram, Open WebUI)? Why not pick one?

Because they're different tools for different moments, and the
*point* of FITT is that the same brain is reachable from each.

- **IDE (Continue, Cursor, Kiro)** — when I'm coding and want
  inline help, edit/apply, context from the file I'm in.
- **Telegram** — when I'm on my phone, away from the desk, want
  to ask a quick question or kick off a long-running task.
- **Open WebUI** — when I'm on a browser on a machine without my
  IDE set up, or when I want the rich UI for a longer session.
- **CLI** (future) — when I'm in a shell and want to pipe stdout
  or scripting.

All four hit the same gateway, same aliases, same memory, same
tools (with per-client approval policies). Session state
persists across interfaces because they share `main` by default.

**Tool comparison:** Kiro is IDE-only. Aider is CLI/terminal.
Claude.ai / ChatGPT are browser-only. FITT's hub-plus-clients
shape exists precisely to avoid picking one.

**Related:** `FITT_ROADMAP.md` Phase 3 (Telegram + Open WebUI);
Phase 1 (IDE via OpenAI-compatible endpoint).

---

### Q: Why does every request have a client tag (`ide`, `telegram`, `webui`, `cli`)?

So tool policy can differ per interface.

The IDE shows diffs natively — writing a file from the IDE is as
safe as writing it in your editor, because you see the diff
before accepting. Auto-approve is appropriate.

Open WebUI runs in a browser on whatever's on your tailnet. Least
trust by default: reads scoped, writes blocked. You can promote
it with explicit policy, but the default assumes "someone else on
my network could hit this."

Telegram is somewhere in between: you see the message, the
approval prompt goes inline, you click approve on your phone.

The tag comes from the Bearer token. Each token in `secrets.yaml`
has a `client:` field:

```yaml
allowed_tokens:
  - name: continue-laptop
    client: ide
    token: ...
  - name: telegram-bot
    client: telegram
    token: ...
```

Untagged tokens default to `webui` — least trust.

**Related:** Phase 4 design, approval + client tag sections.

---

## Deployment

### Q: Why Docker for the hub and native Ollama on satellites?

Different jobs, different fits.

Hub runs three always-on services (gateway, Telegram bot, Open
WebUI) with no GPU needs. Docker Compose gives you:

- One-command up / down.
- Restart policies (auto-restart on crash, on reboot).
- Isolated file trees under one bind-mounted `$FITT_HOME`.
- Portable across Linux, macOS, Windows, QNAP Container Station.

Satellites run Ollama, which is a GPU service. GPU passthrough to
Docker (especially on Windows via WSL2 + NVIDIA Container
Toolkit) is a well-known time sink. Native Ollama on the host OS
talks directly to the driver and Just Works. The 30-minute
Phase 0 goal was "local LLM in the IDE"; we didn't want to spend
it fighting GPU drivers.

**Still open:** satellite-in-Docker is *possible* (and may happen
for pure-CPU satellites). Deferred until someone wants it.

**Related:** `FITT_ROADMAP.md` Phase 0 and Phase 3.5.

---

### Q: Can I run FITT natively, without Docker?

Yes. The gateway, Telegram bot, and Open WebUI are all
**deployment-neutral** — the Python code doesn't know about
Docker. Paths flow from `FITT_HOME` (env var, defaults to
`~/.fitt`). No `if running_in_container()` branches. SSH identity
lands at `$FITT_HOME/ssh/id_ed25519` either way.

Today's `docs/quickstart.md` is Docker-focused because that's the
path the NAS hub uses. A sibling native-install doc is tracked as
a follow-up task — covers systemd on Linux, NSSM on Windows, and
`uv run fitt serve` for dev loops. Until it lands, the
pre-Phase-3.5 Windows instructions (`scripts/install-service.ps1`)
still work.

The code-level rule: if a change ever forces `if container: ...
else: ...`, surface a new env var or config option so both
deployments set it. Docker-specific glue lives in the compose
file and `.env`, not in the Python.

**Related:** steering `project-overview.md` Deployment neutrality
section; Phase 4 follow-up F4 (native-install doc).

---

## Roadmap discipline

### Q: Why "live with it for two weeks" between phases?

Because the roadmap's assumptions about what matters next are
wrong more often than they're right, and two weeks of real use is
the cheapest way to find out.

Specifically: phase N's exit criteria usually read "X works
end-to-end." That's necessary but not sufficient. The real
question is "does X *matter*?" — do I reach for it in daily use?
Two weeks gives the answer before committing a weekend to phase
N+1.

Things I've caught this way (will catch? — the principle is
aspirational as much as observed):

- Feature X ships, turns out to be annoying in a way I didn't
  anticipate → fix X before moving on.
- Feature X ships, turns out I don't actually use it → cut X.
- Feature X ships, turns out the *unspecced* side-effect Y is
  what I use every day → Y gets its own phase.

This is Principle 9. It's the single biggest protection against
building features you won't use.

**Related:** `FITT_ROADMAP.md` principles.

---

### Q: Why is feature X deferred? It looks small.

Small features accumulate. Every one of them has:

- A config surface (now I have to document it).
- A failure mode (now I have to test it).
- A maintenance burden (now it has to survive library upgrades).
- An approval/security implication (now I have to think about
  it).

The roadmap defers features until *absence causes pain*. Three
common reasons:

1. **Not sure it's the right shape yet.** Premature generalization
   is worse than duplication. Defer until the pattern crystalizes.
2. **Depends on a phase that hasn't shipped.** A WebUI for
   approval depends on Task 9 (Telegram approval UI) being wired
   first.
3. **Live-with-it principle.** Maybe the feature's unnecessary.
   Two weeks of use will reveal it.

A "follow-ups" section in each phase's tasks.md tracks what's
deferred, why, and the condition that would promote it. Present
example: Phase 4 F1-F4 (SSH config file, per-project shell, ssh
test upgrade, native-install doc).

**Related:** each phase's tasks.md "Follow-ups" section.

---

## Failure modes

### Q: Why did X break silently and how do I know what to fix? Also — why isn't there a default that just works?

Silent failures are the worst kind — a user hits them, sees
nothing obvious in the logs, has no idea what to change, and
often can't even describe the problem. The first question,
"what's broken," is easy when the system tells you. It's hard
when the system pretends everything is fine.

FITT's rule: **fail loud on detectable misconfigurations**.
Three layers:

1. **Boot-time warnings.** When a config looks off but still
   technically valid (e.g. a token with no `client:` tag),
   log a WARNING with a pointer to the fix. Don't refuse to
   boot if the config still produces a working-ish system —
   that breaks upgrades. Do make the warning loud enough to
   notice in a log tail.
2. **Request-time errors.** When a runtime signal disagrees
   with config (e.g. `X-FITT-Client: telegram` header but
   the token is tagged `ide`), return 400 with **both values
   visible** in the message so operators can tell what to
   reconcile.
3. **Auto-detect when possible.** The Telegram bot sends
   `X-FITT-Client: telegram` on every request. That means an
   operator who forgets to tag the bot's token in
   `secrets.yaml` still gets correct behaviour — the system
   figured it out without asking. Only fall back to config
   when the runtime signal isn't present.

**About defaults.** Safe defaults are tempting
("untagged token → webui least-trust") and often correct,
but they can hide problems. In the Phase 4 bring-up the
"untagged → webui" default meant the Telegram bot's chat
requests got tagged `webui`, its approvals were stored as
`webui`, its poller was asking for `telegram`, and nothing
matched. The gateway was fine. The bot was fine. Their
inability to agree was invisible because the default seemed
fine.

The fix wasn't to pick a different default — no default
could've known the bot should be `telegram` without being
told. The fix was to let the bot *tell* the gateway who it
was (header), and to warn about the ambiguous config at boot
so future operators know what they're leaving on the table.

**Related:** `FITT_ROADMAP.md` principle 11; `auth.py` for the
X-FITT-Client resolution rules; Phase 4 tasks.md commit history
for the bug that motivated this entry.

---

Questions should be the concern someone arrives at the code with,
not the answer framed as a leading question. Structure:

```markdown
## Q: <the concern, framed as it would arrive>

<1-3 paragraphs on why the concern is reasonable — often "tool X
does it that way, why not us">

<What FITT does, grounded in the decision, with references.>

<Still open / tracked-elsewhere items, if any.>

**Related:** <pointers to design docs, roadmap sections, other
FAQs>
```

Keep each entry scoped to one decision. If it grows past ~200
lines or starts covering architecture, move the meat into the
design doc and leave a short pointer here.
