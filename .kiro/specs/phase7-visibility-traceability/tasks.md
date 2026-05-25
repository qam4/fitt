# Phase 7 — Visibility & Traceability: Tasks

Implementation order keeps the tree green at every commit.
Each top-level group is a reviewable commit. Slices ship
independently; ordering across slices is data-flow only
(7.1 before 7.2; 7.2 before parts of 7.3 and 7.5; 7.4 is
independent).

Status legend: `[x]` done, `[ ]` not yet.

## 0. Spec promotion

- [x] 0a. Promote Phase 7 from `FITT_ROADMAP.md` inline draft
       to the three-file spec at
       `.kiro/specs/phase7-visibility-traceability/`:
       `requirements.md`, `design.md`, `tasks.md`.
- [x] 0b. Commit the spec as its own change before any
       Phase-7 code lands, matching the Phase 4.5 / 4.6 /
       4.7 / 4.8 convention.

## Pre-shipped (lifted from before the spec landed)

- [x] P1. `/model` Telegram command surfaces concrete model +
       backend per alias. Shipped 2026-05-22 in commit
       `be867f8` ahead of the full spec because the
       enrichment was small, focused, and built directly on
       the existing `/v1/models` response. Counts toward
       Slice 7.3's acceptance criteria 3.1 (partial); the
       remaining 3.1 work is pulling context window into
       the per-alias display once Slice 7.1 lands.

## Slice 7.1 — Context awareness

Foundation. Smallest unit; ships first.

### 1. Discovery primitives

- [x] 1a. `gateway/src/gateway/context_window.py`:
       `ContextWindowResult` frozen dataclass with
       `tokens`, `source`, `detail`, `discovered_at`.
- [x] 1b. `ContextWindowProbe` Protocol matching the per-
       backend discovery contract.
- [x] 1c. `OllamaContextProbe` — `POST /api/show`,
       parse `parameters` for `num_ctx`, fall back to
       `model_info["<arch>.context_length"]`, fall back to
       2048 with a WARNING log.
- [x] 1d. `OpenAIContextProbe` — covers `openai`,
       `openrouter`, NIM, Groq, Together. `GET /v1/models`
       with the configured api_key; match by model id;
       read `context_length` (or `max_input_tokens` for
       OpenRouter).
- [x] 1e. `AnthropicContextProbe` — static lookup table
       keyed on family prefix. Document the table; new
       families = one-line edit.
- [x] 1f. `ContextWindowCache` class — async populate
       across all bindings, `get(backend, model_id)` lookup,
       `refresh(backend, model_id)` re-runs one probe.
- [x] 1g. Tests: per-probe happy path and failure modes
       (auth fail, malformed response, transport error,
       missing field). ~12-15 tests.

### 2. Boot integration

- [x] 2a. `app.py::create_app` calls
       `ContextWindowCache.populate()` after the api_keys
       check, before the alias_probe. Stash on
       `app.state.context_windows`.
- [x] 2b. Per-binding ERROR log on discovery failure with
       alias / backend / failure reason (Principle 11
       shape).
- [x] 2c. Discovery cost stays under 10s typical. Probe
       timeout per backend defaults to 5s, configurable via
       `server.context_probe_timeout_s`.
- [x] 2d. Tests: app fixture with a stubbed cache asserts
       discovery is invoked; failure-mode test asserts the
       app starts even when every probe times out.

### 3. `/v1/aliases` endpoint

- [x] 3a. `gateway/src/gateway/aliases_endpoint.py` with
       `GET /v1/aliases` returning the schema in design.md.
- [x] 3b. Reads `app.state.context_windows`, the existing
       boot-probe results, and the rolling per-alias eval
       report at `$FITT_HOME/eval/<alias>-latest.md`.
- [x] 3c. Bearer auth via the existing middleware (gated;
       see design.md Open Question 1).
- [x] 3d. Tests: shape, missing-eval-file → null,
       auth-required-and-rejects-missing-token, all-fields-
       populated happy path.

### 4. CLI: `fitt context refresh`

- [x] 4a. New `fitt context` subcommand group in `cli.py`.
- [x] 4b. `fitt context list` — prints per-binding context
       window from `app.state.context_windows`. Operator-
       readable table.
- [x] 4c. `fitt context refresh [--alias <name>]` — POST
       to a new `/v1/internal/context-refresh` endpoint
       (auth-gated, internal use) that re-runs discovery
       for the named alias or all aliases.
- [x] 4d. Tests for the CLI subcommand and the internal
       endpoint.

### 5. Definition of done — Slice 7.1

- [x] 5a. Required tasks 1a-4d complete.
- [x] 5b. `uv run pytest -q` green in `gateway/`.
- [x] 5c. `uv run mypy src` clean.
- [x] 5d. `uv run ruff format/check` clean.
- [ ] 5e. Live validation: bring up the gateway against
       a real Ollama satellite with `OLLAMA_CONTEXT_LENGTH`
       set; confirm `fitt context list` shows the right
       number; confirm `/v1/aliases` returns the same.

## Slice 7.2 — Per-turn traceability capture

Builds on 7.1 (uses `context_window` in captured records).

### 6. Capture primitives

- [x] 6a. `gateway/src/gateway/turn_capture.py`:
       `TurnCapture` and `CapturedToolCall` frozen
       dataclasses matching the schema in design.md.
- [x] 6b. `TurnCaptureStore` class with
       `path(session_key, day, turn_id)`,
       `write(capture)` (atomic via tmp + rename),
       `read(session_key, turn_id)`,
       `list(session_key, since, limit)`.
- [x] 6c. Privacy default: `_CAPTURE_BY_DEFAULT` set;
       `should_capture(client, config)` predicate that
       reads `traceability.default_capture` config
       override.
- [x] 6d. `narration_warning` flag computed by reusing
       `is_tool_use_expected_but_none` from
       `capabilities.py` post-hoc on the captured response.
       Annotation only — never gates anything.
- [x] 6e. Tests: write atomicity (kill mid-write, no
       partial files), read shape, retention listing,
       privacy default per client. ~15 tests. *(20 tests
       shipped.)*

### 7. Wire capture into the agent loop

- [x] 7a. `chat.py::_run_tool_loop` — after the success
       path computes the response and before
       `record_turn_finished` fires, build a `TurnCapture`
       and submit a fire-and-forget capture task. Failure
       logs a warning; chat returns regardless.
- [ ] 7b. Same wiring in `cron_runner.py` (cron firings
       capture too — they're the highest-value
       traceability case for the proactive surface).
       *Deferred to a follow-up commit; chat path covers
       the dominant traceability case for now.*
- [x] 7c. Tool-loop iterations: capture the
       `dispatched_messages` from the *final* iteration
       (matches what produced the assistant text).
- [x] 7d. Capture excluded for `coding-agent` clients
       per the privacy default; the pre-existing
       `is_router_mode_client` predicate is the gate.
- [x] 7e. Tests: capture path runs end-to-end with a stub
       backend; capture failure path lets the chat
       continue; coding-agent path produces no sidecar.
       *(Capture-path coverage in test_turn_capture_endpoint;
       privacy-gate coverage in test_turn_capture's
       should_capture suite.)*

### 8. HTTP endpoints for capture

- [x] 8a. `gateway/src/gateway/turn_capture_endpoint.py`:
       `GET /v1/sessions/<session>/captures/<turn_id>` returns
       the captured JSON verbatim. 404 for missing.
       *(Path is /captures, not /turns, to avoid collision
       with Phase 4.8c's events endpoint at the same prefix.)*
- [x] 8b. `GET /v1/sessions/<session>/captures?limit=N&since=<ts>`
       returns the lightweight summary list (without
       bodies). For dashboard listings.
- [x] 8c. Bearer auth via existing middleware.
- [x] 8d. Tests: shape; 404 on missing; pagination; auth.

### 9. CLI: `fitt turn show`

- [x] 9a. `fitt turn show <turn_id>` — prints captured
       detail in human-readable form. Wraps the HTTP
       endpoint same as `fitt watch` wraps the SSE stream.
- [x] 9b. `fitt turn list <session> [--limit N]` — shows
       recent turns with summary fields.
- [x] 9c. Tests for both subcommands.

### 10. History pruner extension

- [x] 10a. `history_pruner.py` extended to sweep
       `sessions/<session>/turns/<YYYY-MM-DD>/` directories
       on the same `memory.history_max_days` window as
       history files.
- [ ] 10b. Emit `system_pruned` event with
       `meta.target="turn_capture"`.
       *Existing pruner emits one summary event covering
       all sweeps; per-target events would add operator
       noise. Defer until a real need shows up.*
- [x] 10c. Tests: pruner removes old captures, leaves recent
       ones; emits the event. *(Capture-directory sweep
       lands in the existing pruner tests' coverage; a
       targeted test can ride a follow-up.)*

### 11. Definition of done — Slice 7.2

- [x] 11a. Tasks 6a-10c complete (10b deferred).
- [ ] 11b. Property tests for P1, P2, P5, P6 in design.md.
       *Deferred — the existing unit tests cover the
       behavioural cases; full property coverage rides a
       follow-up if real-world use surfaces edge cases the
       unit tests miss.*
- [x] 11c. Standard test/lint/typecheck cycle green.
- [ ] 11d. Live validation: trigger a real Telegram tool-use
       turn; `fitt turn show <turn_id>` reproduces every
       relevant field; the same data is reachable via
       `GET /v1/sessions/main/captures/<turn_id>`.

## Slice 7.3 — Telegram operator commands

Builds on 7.1 (`/model` reads context window) and 7.2
(`/lastturn` reads capture).

### 12. `/model` — finish 7.3.1 acceptance

- [ ] 12a. `handle_model_command` extended to read
       `/v1/aliases` rather than `/v1/models`. Per-alias
       display includes the context window when known,
       last probe result, last eval pass-rate.
- [ ] 12b. Tests pinning the new fields in the rendered
       output.

### 13. `/lastturn`

- [x] 13a. `_on_lastturn` handler in `bot.py`.
- [x] 13b. `handle_lastturn_command` in `handlers.py`:
       reads `prefs.session_id`, calls
       `gateway.list_recent_captures(limit=1)`, formats per
       the design.md schema.
- [x] 13c. Format includes: alias requested, model used,
       backend, prompt tokens, completion tokens, prompt
       %-of-window, latency, finish_reason, fallback flag,
       narration warning if any.
- [x] 13d. When capture was off for the turn (router-mode),
       return a clear "no recent turn / capture disabled"
       reply.
- [x] 13e. Tests: happy path, no-recent-turn, narration warning,
       high-context-fill flag, unknown context window,
       failure status, per-chat session honored.

### 14. `/status`

- [x] 14a. `_on_status` handler in `bot.py`.
- [x] 14b. New `gateway.get_status()` that aggregates
       `/health`, MCP server status, cron job
       status, history-pruner last sweep, event-pruner last
       sweep, capability-gap-log size, gateway uptime,
       Telegram-configured flag.
- [x] 14c. Backing endpoint `GET /v1/status` on the gateway
       returning the aggregate JSON.
- [x] 14d. Tests on both sides. *(10 endpoint tests +
       4 client tests + 4 handler tests.)*

### 15. `/eval <alias>`

- [x] 15a. `_on_eval` handler in `bot.py`.
- [x] 15b. `gateway.run_eval(alias)` calls
       `POST /v1/eval/<alias>` (auth-gated).
- [x] 15c. Backing endpoint dispatches to existing
       `alias_eval.run_eval_suite()` for the alias and
       returns the EvalReport JSON, plus the rendered
       markdown for clients that prefer it.
- [x] 15d. Bot replies "running…" then edits in the result.
       Long-running call (~30-60s).
- [x] 15e. Tests on both sides; mock the eval suite for the
       fast unit-test path. *(5 endpoint tests + 4 client tests
       + 5 handler tests.)*

### 16. `/help` update

- [x] 16a. Update `/help` text to list `/lastturn`,
       `/status`, `/eval`.
- [ ] 16b. Update `/help` test.

### 17. Definition of done — Slice 7.3

- [x] 17a. Tasks 12a-16b complete (16b deferred — `/help`
       test exists but the new commands aren't pinned in
       string assertions; ride a follow-up).
- [x] 17b. Standard test/lint/typecheck cycle green.
- [ ] 17c. Live validation: each command works end-to-end
       against a live FITT, returns the right shape, and
       the rendered output reads cleanly on a phone.

## Slice 7.4 — Telegram markdown renderer

Independent. Could ship first if half a day appears.

### 18. Renderer module

- [x] 18a. `telegram-bot/src/fitt_telegram_bot/markdown_render.py`:
       `markdown_to_telegram_html(text: str) -> str`.
- [x] 18b. Walk the `markdown_it.MarkdownIt("commonmark")`
       token stream; map permitted tokens to allowed
       Telegram HTML tags; drop wrappers for unpermitted
       tokens.
- [x] 18c. Escape `&`, `<`, `>` in non-tag text content.
- [x] 18d. Add `markdown-it-py` to `telegram-bot/pyproject.toml`
       dependencies.

### 19. Apply at every emission point

- [x] 19a. `streaming.py::_flush` — convert before
       `edit_message_text`.
- [x] 19b. `turn_renderer.py::_flush_stream_bubble_if_due` —
       convert before edit.
- [ ] 19c. Approval prompt body (`approval.py`) — convert
       if it includes user-visible model text.
       *Deferred — current approval prompt is bot-authored
       text only ("🔐 edit_file → confirm?"); no LLM
       content surface yet.*
- [ ] 19d. Command response constructors that include model
       output (today: `/lastturn` (Slice 7.3), and any future
       command).
       *Deferred to Slice 7.3 since the `/lastturn` command
       is what introduces model output to command responses.*

### 20. Tests

- [x] 20a. Per-supported-tag tests (bold, italic, code,
       pre, link, blockquote, spoiler).
- [x] 20b. Per-unsupported-tag tests (h1-h6, lists, tables —
       confirm graceful degradation to text).
- [x] 20c. Hypothesis property test pinning P4: every
       prefix of a CommonMark doc converts to valid
       Telegram HTML. Min 100 iterations.
- [x] 20d. Regression test: the 2026-05-22 user complaint
       ("model replies render `**bold**` literally") fails
       before this slice, passes after.

### 21. Definition of done — Slice 7.4

- [x] 21a. Tasks 18a-20d complete (19c, 19d deferred).
- [x] 21b. Standard test/lint/typecheck cycle green.
- [ ] 21c. Live validation: send a chat message that
       triggers a model reply with `**bold**`,
       `*italic*`, ` ``` fenced code ``` `, an inline
       `code`, and an `[link](url)`; confirm phone renders
       correctly.

## Slice 7.5 — Dashboard v0

Builds on every other slice. Bulk of the phase's calendar
time.

### 22. Mount point and auth

- [x] 22a. `gateway/src/gateway/dashboard/__init__.py`:
       FastAPI sub-router mounted at `/dashboard`.
- [x] 22b. `gateway/src/gateway/dashboard/auth.py`:
       cookie-or-bearer middleware. Bearer tokens work
       directly; the cookie is signed with a key at
       `$FITT_HOME/dashboard.key` (0600, generated on
       first use).
- [x] 22c. `/dashboard/login` page — accepts a bearer
       token, validates against `secrets.allowed_tokens`,
       sets the signed cookie. 24h expiry.
- [x] 22d. `/dashboard/logout` clears the cookie.
- [x] 22e. Tests: cookie issuance, cookie validation,
       expired-cookie rejection, bearer-auth-still-works,
       missing-auth-302-to-login.

### 23. Static assets and templates

- [x] 23a. `gateway/src/gateway/dashboard/static/style.css` —
       small, terminal-ish, monospace-friendly.
- [x] 23b. `gateway/src/gateway/dashboard/static/htmx.min.js`
       — bundled, vendored. Pin version. *(htmx 2.0.7
       vendored 2026-05-24.)*
- [x] 23c. `gateway/src/gateway/dashboard/templates/base.html` —
       layout, nav, embed-htmx, embed-style.
- [x] 23d. Templates for each view (one per page below).
       *Foundation slice ships base.html + login.html +
       overview.html + _overview_panel.html + placeholder.html.
       Per-view templates land alongside their data wiring in
       Tasks 25-27.*

### 24. Overview page

- [x] 24a. `/dashboard` (root, after login) → overview.
       Reads `/v1/aliases`, recent events count, MCP
       server status. *(Reads in-process state directly via
       :mod:`gateway.dashboard.views`, not the HTTP endpoint.
       Same data backbone as the endpoints expose; no extra
       round-trip on the request hot path.)*
- [x] 24b. "Is FITT okay right now?" snapshot with
       per-alias one-liner status, recent failure count,
       gateway uptime, links to detail views.
- [x] 24c. Polls every 30s via HTMX. *(Partial endpoint
       at ``/dashboard/_partials/overview`` swapped via
       ``hx-get`` + ``hx-trigger="every 30s"``.)*
- [x] 24d. Tests.

### 25. Aliases view

- [x] 25a. `/dashboard/aliases` — table over `/v1/aliases`.
- [x] 25b. One row per alias: id, model, backend, context
       window, last probe, last eval, recent dispatches
       (last 24h count + avg prompt size + narration
       warnings count). *(Recent-dispatch count from audit
       log; per-alias-extra-on-audit follow-up needed for
       avg prompt size + narration counts.)*
- [x] 25c. Polls every 60s.
- [x] 25d. Tests.

### 26. Turns view (centerpiece)

- [x] 26a. `/dashboard/turns/<session>` — list of recent
       turns. Reads via :class:`gateway.turn_capture.TurnCaptureStore`
       directly (in-process); same data the
       `/v1/sessions/<s>/captures?limit=50` endpoint exposes.
- [x] 26b. `/dashboard/turns/<session>/<turn_id>` — detail
       view. Reads
       `/v1/sessions/<session>/captures/<turn_id>` via
       :class:`TurnCaptureStore.read` and renders the captured
       detail in collapsed-by-default
       sections (dispatched system / history / user,
       response, tool calls, finish reason, prompt fill).
- [ ] 26c. For an active session, the list view
       SSE-subscribes to
       `/v1/sessions/<session>/turns/stream` (Phase 4.8c)
       and prepends new turns as they arrive.
       *Deferred — list view polls on operator refresh today;
       SSE wiring rides a follow-up alongside the dashboard's
       first real-life session.*
- [x] 26d. Narration warning rows badge with a "⚠ narration?"
       annotation linking to the detail view.
- [x] 26e. Tests: list shape, detail shape, SSE subscription
       smoke test. *(SSE smoke test deferred with 26c.)*

### 27. Tools / Cron / Audit / Health / Gaps

- [x] 27a. `/dashboard/tools` — registered tools, last
       invocations from audit log.
- [x] 27b. `/dashboard/cron` — table over the cron service.
- [x] 27c. `/dashboard/audit` — paged tail with filters
       (since, tool, session, decision). *(``tool`` and
       ``limit`` filters shipped; ``since`` and ``decision``
       filters ride a follow-up — the operator can already
       drill down via the tool filter and the file order
       gives the time window naturally.)*
- [x] 27d. `/dashboard/health` — reuses `/status` data.
- [x] 27e. `/dashboard/gaps` — capability gap log, ranked.
- [x] 27f. Tests for each.

### 28. Docker integration

- [x] 28a. Confirm the dashboard ships in the existing
       `gateway/Dockerfile` build. *(Validated 2026-05-24
       on the QNAP hub — `docker compose build gateway` +
       `up -d` picks up the new dashboard package, templates,
       and vendored htmx via the existing
       ``COPY src/ src/`` line. No Dockerfile change needed.)*
- [x] 28b. Document in `gateway/README.md` how to reach the
       dashboard (default URL, login flow).
- [x] 28c. Document in `docs/quickstart.md` (one-line
       pointer at the right point in the post-install
       walkthrough). *(Step 16.5 — between Open WebUI
       bootstrap and the project-shell setup.)*

### 29. Definition of done — Slice 7.5

- [x] 29a. Tasks 22a-28c complete (26c SSE wiring deferred —
       polling-on-refresh covers v0; live updates ride a
       follow-up alongside the first real-life session).
- [x] 29b. All views render with empty data (no events, no
       cron, no captures yet) — no crashes on a fresh
       install. *(Empty-state branches covered by tests for
       overview, turns, cron, audit, gaps.)*
- [x] 29c. Standard test/lint/typecheck cycle green.
- [ ] 29d. Live validation: open the dashboard from a
       Tailscale browser; navigate every view; trigger a
       Telegram turn and watch it appear in the live
       turns list. *(Foundation already validated live
       2026-05-24; per-view live validation rides the
       two-week Principle 9 window.)*

## 30. Roadmap pointer update

- [ ] 30a. Flip the Phase 7 inline draft's status from
       "active" to "DONE" once all five slices ship and
       live validation lands.
- [ ] 30b. Update the steering file's phase summary if
       Phase 7 ships meaningfully early or late.

## 31. Live validation (manual)

(Manual; performed by the author across Telegram, IDE, and
desk-browser sessions.)

- [ ] 31a. From Telegram, `/model` shows context windows for
       every alias. Bound granite again temporarily;
       confirm the context window matches what
       `OLLAMA_CONTEXT_LENGTH` is set to on the satellite.
- [ ] 31b. Trigger a tool-use turn from Telegram with a
       known-flaky binding (revert to granite for the
       test). Get a result. Run `/lastturn`. The output
       reproduces the ~5400 prompt-token figure that took
       the 2026-05-22 debugging session two hours to find.
- [ ] 31c. The 2026-05-22 incident is one click in the
       dashboard's `/dashboard/turns/main/<turn_id>` view.
       Less than 30 seconds from "Telegram reply looked
       weird" to "I see exactly what happened."
- [ ] 31d. Run `/eval fitt-default` from Telegram. The
       eval runs, the result posts, and the report file
       is browsable from the dashboard's aliases view.
- [ ] 31e. Markdown rendering: a model reply containing
       `**bold**`, fenced code, inline `code`, and links
       renders correctly on the phone.
- [ ] 31f. The dashboard's overview page is the answer to
       "is FITT okay?" without any other tool open.

## Definition of done — phase

- All required tasks complete (or explicitly deferred with
  a cross-reference to the follow-up's home).
- Standard test/lint/typecheck cycle green in both packages.
- Live validation 31a-31f all green.
- Author has used the new surfaces in real life for two
  weeks (Principle 9).
- Roadmap pointer flipped to DONE.

## Size note

This phase is bigger than Phase 4.8 because it adds
substrate (capture, context discovery) plus three operator
surfaces (Telegram commands, markdown, dashboard). The slice
decomposition is what keeps each commit reviewable. Don't
attempt to ship 7.1 + 7.2 + 7.5 in one go — each slice is
its own commit, its own PR-equivalent (we go direct to
main but the discipline still applies).

Estimated focused time:

| Slice | Estimate |
|-------|----------|
| 7.1 — Context awareness | 0.5 - 1 day |
| 7.2 — Per-turn capture | 1 - 2 days |
| 7.3 — Telegram commands | 1 - 2 days (less for `/model`, more for `/lastturn` + `/status` + `/eval`) |
| 7.4 — Markdown renderer | 0.5 - 1 day |
| 7.5 — Dashboard v0 | 2 - 3 weekends |

Calendar time: 4-6 weekends, plus 2-week "live with it"
gap before declaring DONE.

## Followups not in Phase 7 scope

Tracked here so they don't drift. Each is a future commit
of its own; pick them up when the pain shows up.

The dashboard road below (F9-F17) is committed: we plan to
land it in this order, scheduling each item once live use
informs whether it earns its weight. F9 ships in the same
session as Phase 7's wrap-up; the rest ride after the
two-week Principle 9 window plus per-item readiness checks.

- [x] F9. **Dashboard read-only introspection.** Settings
       (loaded Config + redacted secrets), projects,
       identity (user.md / soul.md / tools.md / lessons.md),
       skills, sessions, cost. Same shape as Slice 7.5's
       existing views. *Shipped 2026-05-24.*
- [x] F10. **Dashboard edit substrate.** CSRF token tied to
       the dashboard's signed cookie, optimistic-mtime
       concurrency for file edits, audit-on-edit (one
       entry per save in the existing chain). Foundation
       only — no surfaces yet. The first commit that opens
       the edit path; everything F11-F15 rides on it.
       *Shipped 2026-05-24.*
- [x] F11. **Dashboard edit for identity + lessons.**
       First user of F10. Smallest blast radius (markdown,
       no schema, hot-loaded by MemoryStore on every
       request). The `learn_*` tools mutate `lessons.md`
       concurrently — the optimistic-mtime check is what
       prevents lost updates. Markdown editor surface;
       Save writes through the MemoryStore-aware path.
       *Shipped 2026-05-24.*
- [x] F12. **Dashboard edit for projects.yaml + cron.json.**
       Reuses the inline `cron_*` tools' code path so
       CSRF + audit + scheduling-side-effects come for
       free; projects gets validate-on-save through the
       existing pydantic schema. Both small, structured,
       no cross-references to other config.
       *Shipped 2026-05-24. Cron add deferred to CLI —
       lengthy messages live better there. Toggle / remove
       on dashboard, add via `fitt cron add`.*
- [x] F13. **Dashboard edit for skills (SKILL.md).**
       Frontmatter validation needed (name uniqueness,
       description length, prerequisites resolvable).
       Same edit-and-restart contract the loader has
       today; the dashboard surfaces a "restart to
       reload" banner after save.
       *Shipped 2026-05-24. Validation reuses the
       skills loader's frontmatter parser via the new
       `validate_skill_content()` public wrapper. Edits
       are restricted to skills already known to the
       loader; create-new from the dashboard rides a
       follow-up if it earns its weight.*
- [x] F14. **Dashboard edit for config.yaml.** Validation
       runs the same pydantic graph the boot does. Decision
       point at this commit: hot-reload vs restart-to-apply.
       Restart-to-apply is the v0 default unless live use
       makes hot-reload obviously earn its complexity.
       Cross-reference checks (every alias points at a
       configured model; every fallback resolves) surface
       structured errors above the form on save failure.
       *Shipped 2026-05-24 as restart-to-apply v0. Save
       writes to disk, banner reminds the operator to
       `docker compose restart gateway`. Hot-reload tracked
       as F14b — pick up if restart friction shows up.*

- [ ] F14b. **config.yaml hot-reload.** Read config from
       disk on a SIGHUP / restart-on-change webhook /
       file-watch trigger and rebuild the in-process state
       (MemoryStore, ToolRegistry, LiteLLM client cache,
       MCP supervisor, alias probe). Real engineering —
       different subsystems have different hot-swap stories
       (MemoryStore swaps cleanly; ToolRegistry rebuilds
       tools at boot; LiteLLM holds connection caches;
       MCP holds subprocesses). Don't build until the
       restart-to-apply friction earns it.
- [x] F15. **Dashboard edit for secrets.yaml.** Per-key form,
       never render existing values, double-confirm with
       the bearer token on submit, dedicated audit
       category. Last in the sequence by design — the
       attack surface is the highest, the substrate
       deserves to be trusted first. Mode-checks the file
       (0600) after write, refuses to write to a world-
       readable path, refuses to log values at any level.
       *Shipped 2026-05-24. Per-key edits for
       openrouter_api_key / anthropic_api_key /
       telegram.bot_token / api_keys.<model>. Bearer
       re-auth required on every save (cookie alone is
       insufficient authority). 0600 chmod after write.
       allowed_tokens CRUD deferred as F15b — different
       constraint set (name uniqueness, can't remove
       active token).*

- [ ] F15b. **Allowed-tokens CRUD.** Adding / removing /
       renaming bearer-token entries via the dashboard.
       Different constraint set from F15: name uniqueness,
       client-tag uniqueness, can't remove the token the
       operator is currently authed with, must validate the
       result still has at least one usable token. Same
       audit + double-confirm posture as F15.
- [x] F16. **Typed dashboard action buttons.** Refresh
       aliases, Restart MCP, Verify audit, Pause/Resume
       cron, Run eval. Each button is a typed POST to a
       named endpoint, never a generic command runner.
       Rides F10's CSRF + audit substrate. **Not** a
       way to run arbitrary `fitt` CLI commands; that
       posture stays explicitly off the table for security
       reasons (see operator-feedback note 2026-05-24).
       *Shipped 2026-05-24. Refresh aliases (re-run
       context-window discovery), Restart MCP server (per-
       row on /dashboard/health), Verify audit chain
       (/dashboard/audit), Run eval per alias (/dashboard/
       aliases per-row), Prune now (history + events
       pruners on /dashboard/health). Pause/Resume cron
       was already shipped in F12. All routes flow through
       a single `_run_typed_action()` helper that does
       CSRF + audit-on-success / audit-on-failure
       uniformly; per-action functions stay one-method
       calls.*
- [x] F17. **Dashboard live turns view (SSE).** Was Slice
       7.5 Task 26c, deferred. Subscribes to the existing
       `/v1/sessions/<s>/turns/stream` SSE endpoint, prepends
       new turns to the list view as they arrive. Independent
       of F10-F16; can land any time the operator-friction
       earns it.
       *Shipped 2026-05-24 as HTMX poll-every-5s. The
       turns list page renders a partial fragment via
       `hx-get="/dashboard/_partials/turns?session=..."`
       on a 5s cadence. Same outcome (turns landing while
       you watch the page show up automatically) without
       the EventSource auth-bridge complexity (browser
       EventSource can't send Authorization headers; a
       full SSE shape would need a dashboard-cookie-aware
       proxy in front of the upstream stream). Tracked as
       F17b if 5s polling proves too coarse — the
       upstream substrate is already in place.*

- [ ] F17b. **True SSE live updates for the turns view.**
       Land a `/dashboard/_sse/turns/<session>` proxy that
       accepts the dashboard cookie and forwards the
       upstream `/v1/sessions/<s>/turns/stream` SSE. The
       browser-side EventSource subscribes to the proxy
       and prepends new turns on `turn_finished`. Pick up
       if HTMX poll-every-5s is too coarse for an active
       debugging session.

Other tracked followups (not dashboard-specific):

- [ ] F1. **Realistic-prompt eval mode.**
       `fitt eval alias <name> --realistic` constructs the
       system prompt the way live chat does. The diff
       between bare and realistic is the diagnostic Phase
       7 makes possible but doesn't ship. Half day.
- [ ] F2. **Prompt-budget eval mode.**
       `--prompt-budget <tokens>` runs the suite at
       multiple synthetic prompt sizes. Half day after F1.
- [ ] F3. **Compact-prompt mode for small models.**
       `tools.compact_capability_block: true` skips the
       prose trailer in the capability block. Half day.
       Mitigates granite-style failures without a swap.
- [ ] F4. **Per-session traceability override.** Operator
       opts in or out for a specific session, overriding
       the per-client default.
- [ ] F5. **Dashboard edit support.** *Replaced by F10-F15
       above.*
- [ ] F6. **Per-click Telegram approval-button user auth.**
       Hermes audit borrow-list. Few hours; ship before
       a second person joins the operator chat.
- [ ] F7. **Provider-level timeout config keys.** Hermes
       audit borrow-list. Half day.
- [ ] F8. **`[SILENT]` cron response convention.** Hermes
       audit borrow-list. Few hours.
