# Phase 4.8 — Visibility Proxies: Tasks

Five sub-phases. Each one lands independently; 4.8a is the
dependency for everything else.

## Sub-phase 4.8a — Per-turn event stream backend

- [ ] Create `gateway/src/gateway/turns.py` with
      `TurnEvent` dataclass, `TurnLog` class (append, read,
      path helpers). Mirror the shape of
      `gateway/src/gateway/events.py`.
- [ ] Pin the event-kind schemas in a module constant + a
      set of typed `new_*_event` constructors (one per kind
      from design.md § "Event-kind schemas").
- [ ] Add `turn_id: str | None = None` to `ToolContext`.
- [ ] Wire `record_turn_started` / `record_turn_finished` in
      `chat.py` around the tool-loop entry.
- [ ] Wire `record_llm_call_started` / `record_llm_call_completed`
      in `agent_loop.py` around the dispatch call.
- [ ] Wire `record_tool_call_planned` in `execute_tool_call`
      at entry; `record_tool_call_executed` at return.
- [ ] Wire `record_approval_requested` in
      `ApprovalMiddleware.request_approval`.
- [ ] Wire `record_approval_decided` in
      `resolve_approval` + timeout branch of
      `_request_and_wait`.
- [ ] Extend the existing `record_narrated_tool_call` /
      `record_gap` in `agent_loop.py`
      to also emit turn events.
- [ ] Generate `turn_id` once per chat request in `chat.py`
      and on every cron firing in `cron_runner.py`; pass to
      the tool context.
- [ ] Handle IO failures as non-fatal warnings per P3.
- [ ] Hook the history pruner to walk `turns.jsonl` (mtime-
      based if per-file, date-based if we decide on per-day
      rotation — see open question 1).
- [ ] Config knobs under `memory:` (or decided location) for
      retention.
- [ ] Unit tests for `TurnLog` mirroring `test_events.py`.
      Include the hypothesis property test for P1 (one line
      per append, valid JSON).
- [ ] Integration test: full agent-loop turn with stubbed
      LLM produces the expected event sequence.
- [ ] IO-failure test: unwritable `turns.jsonl` logs a
      warning and the turn completes anyway.

**Exit criteria:** a tool-using turn in Telegram writes a
valid `turns.jsonl` with all the expected event kinds in
order. `ruff` / `mypy` / `pytest` clean.

## Sub-phase 4.8b — `fitt watch` CLI renderer

- [ ] Create `gateway/src/gateway/cli_watch.py` with the
      renderer and tail loop.
- [ ] Implement the format from design.md § "CLI `fitt
      watch`". Fixed-width kind column, key-sorted meta
      rendering, two-level dict flatten with `{...}`
      truncation.
- [ ] Color via `rich` (already a dependency); ok=green,
      warn=yellow, error=red per design.md.
- [ ] Tail loop uses `TurnLog.read(since=<last_ts>)` with
      two-second sleep between polls.
- [ ] `fitt watch <session>` and `fitt watch
      --session-active` (latest-by-turn).
- [ ] Works under `docker compose exec gateway fitt watch
      ...` — test that path.
- [ ] Output-format unit tests with synthetic
      `turns.jsonl` files.
- [ ] Tail-behavior test with a file being appended by a
      background task.

**Exit criteria:** `fitt watch` renders a live session's
events line-by-line, updates within 2s of a new event
landing. Clean exit on Ctrl-C.

## Sub-phase 4.8c — HTTP read endpoints

- [ ] Extend (or create) `gateway/src/gateway/events_endpoint.py`
      / rename to a broader name. Add routes for:
      - `GET /v1/events`
      - `GET /v1/audit`
      - `GET /v1/capability-gaps`
      - `GET /v1/sessions/{id}/turns`
- [ ] Pagination via `since=<ts>` / `limit=<n>`. Response
      shape `{entries, next_since}`.
- [ ] Auth via existing `AuthMiddleware`; no per-endpoint
      ACL in this phase.
- [ ] `TestClient` tests: happy path, 401 on bad token,
      `since` filtering correct, `limit` bounded,
      `next_since=null` at tail.
- [ ] Per-session filter for `/v1/sessions/{id}/turns`
      returns only that session's events.
- [ ] Document the endpoints in `README.md` or a new
      `docs/http-api.md`.

**Exit criteria:** a curl against each endpoint returns
well-formed JSON and matches the CLI's output for the same
filter. Tests clean.

## Sub-phase 4.8d — Static HTML viewer

- [ ] Create `gateway/src/gateway/viewer.py` with the HTML
      page as a module string. HTMX from CDN.
- [ ] Route `GET /v1/events/view?token=<token>` (with
      redirect-from-untokened-URL behaviour if we implement
      that — see open question 4).
- [ ] Inline CSS (readable on phone, monospace for meta).
- [ ] `hx-get` polling every 5s, append to top of list.
- [ ] Meta-detail expand-in-place per row.
- [ ] XSS-safe: HTML-escape all `meta` values before
      insertion.
- [ ] Smoke test: endpoint returns 200, body contains the
      expected list container + script block.
- [ ] Test that XSS attempt in an event's `meta.body`
      renders as escaped text.

**Exit criteria:** load
`http://<hub-tailnet>:8080/v1/events/view?token=...` on a
phone, see events landing every 5 seconds. Survives a
crafted event with `<script>` in its body.

## Sub-phase 4.8e — Telegram `/inbox` command

- [ ] Add a `/inbox` command handler in
      `telegram-bot/src/fitt_telegram_bot/handlers.py`.
- [ ] Calls `GET /v1/events` via the bot's HTTP client with
      its service token.
- [ ] Reuses the Phase 4.5 push-channel event formatters —
      no duplicate formatting logic.
- [ ] Pagination: 10 events per message, inline keyboard
      for navigation.
- [ ] Filter args: `/inbox cron`, `/inbox errors`,
      `/inbox session=<id>`.
- [ ] Unit tests for the handler with a stubbed bot and
      HTTP client.
- [ ] Integration test: post `/inbox`, assert a message
      with recent events.

**Exit criteria:** `/inbox` on the phone returns recent
events paged at 10 per screen. Filter flags narrow the
view. Ctrl-Z via operator testing.

## Cross-cutting

- [ ] Update `FITT_ROADMAP.md` Phase 4.8 entry to reference
      this spec directory once it exists.
- [ ] Log the shipping of each sub-phase in
      `docs/observed-issues.md` as its pain points surface
      fixes.
- [ ] Decide on (and document) the open questions from
      design.md § "Open questions" before committing each
      sub-phase.
