# Phase 4.11 — Web Search Tool: Tasks

## Scope

Two commits, ~half-day total. Ships the `web_search` tool with
the `ddgs` provider as v1, leaving the seams for SearXNG /
Brave-free / Exa to drop in later as one-file additions.

The work is sequenced so the architecture is in place before
the tool ships:

- **Commit 1** ships the ABC, the dispatcher tool, the DDGS
  provider, and the config plumbing — gated behind unit
  tests + integration test. Live wiring (gateway boot
  registers the tool) lands here.
- **Commit 2** ships the property tests, the README + docs
  updates, the `pyproject.toml` dep add, and NAS smoke.

Each commit's tests must pass before the next commit starts.
Commit 2's NAS verification is the final gate.

## Commit 1: Tool, ABC, DDGS provider, integration test

Goal: a fully-wired `web_search` tool registered in the
gateway. Calling it with `query="latest python"` (DDGS mocked)
returns three structured hits.

- [x] 1a. Add `gateway/src/gateway/tools/web_providers/__init__.py`
        with the `WebSearchProvider` ABC, the
        `WEB_SEARCH_REGISTRY` module-level dict, the
        `register_provider()` setter, and the
        `discover_providers()` discovery hook.
- [x] 1b. Add `gateway/src/gateway/tools/web_providers/ddgs.py`
        with `DDGSWebSearchProvider`. Implementation per
        design.md: lazy import inside `search()`,
        normalize ddgs's response fields (`title`, `href`,
        `body`) into the SearchResult shape, cap results at
        `limit`, catch all exceptions, return structured
        success/error envelopes. Module body calls
        `register_provider(DDGSWebSearchProvider())` at
        import time.
- [x] 1c. Add `gateway/src/gateway/tools/web_search.py` with
        `_tool_web_search` (the async dispatcher) and
        `build_web_search_tool(config)` (the `Tool` factory
        that reads `config.web.search_backend` and builds
        the description text dynamically).
- [x] 1d. Add `WebSearchConfig` to `gateway/src/gateway/config.py`
        and a top-level `web: WebSearchConfig =
        Field(default_factory=WebSearchConfig)` on `Config`.
        Pydantic ValidationError on non-string
        `search_backend` exits the gateway non-zero per
        Requirement 4.2.
- [x] 1e. In `gateway/src/gateway/tools/__init__.py`'s
        `build_inline_tools(registry)`, call
        `web_providers.discover_providers()` once and
        append `build_web_search_tool(config)` to the
        returned list. Note: this requires the function
        to gain access to `config`; pick the cleanest
        path (likely thread it through from
        `gateway.app.create_app`).
- [x] 1f. Add `ddgs>=9.0,<10` to gateway's
        `pyproject.toml` `[project] dependencies`. Run
        `uv sync` in `gateway/` and confirm
        `import ddgs` works.
- [x] 1g. Write `gateway/tests/test_web_providers.py` with
        the four ABC + registry unit tests (design.md
        Testing Strategy).
- [x] 1h. Write `gateway/tests/test_web_search_ddgs.py`
        with the seven DDGS provider unit tests
        (design.md Testing Strategy). Mock the `ddgs.DDGS`
        class so no network I/O happens.
- [x] 1i. Write `gateway/tests/test_web_search.py` with
        the eleven dispatcher unit tests (design.md
        Testing Strategy). Use a stub provider registered
        directly into `WEB_SEARCH_REGISTRY` for the happy
        paths; the unknown-backend tests omit the stub.
- [x] 1j. Write `gateway/tests/test_web_search_e2e.py`
        per Requirement 6. Two scenarios (success path,
        error path); same tmp-config + create_app pattern
        as `test_skills_e2e.py`.
- [x] 1k. Run `uv run ruff format src tests`,
        `uv run ruff check src tests --fix`,
        `uv run mypy src`, `uv run pytest -q` in
        `gateway/`. All green before committing.
- [x] 1l. Commit. Suggested message:
        `Phase 4.11/1: web_search tool + ddgs provider`.

## Commit 2: Property tests, README, NAS smoke

Goal: pin the harder properties with hypothesis, document
the operator workflow, verify the loop end-to-end on the
NAS.

- [x] 2a. Write `gateway/tests/test_web_search_properties.py`
        per design.md Testing Strategy. Two hypothesis
        tests, each min 100 iterations, tagged
        `# Phase 4.11, Property 2: Failure isolation`
        and `# Phase 4.11, Property 3: Limit clamping`
        per the conventions doc.
- [x] 2b. Update `configs/config.example.yaml` per
        Requirement 4.4: add the `web:` block with
        `search_backend: ddgs` and one comment sentence
        explaining the field's purpose.
- [x] 2c. Update `gateway/README.md` config reference
        with a "Web search (Phase 4.11)" subsection
        documenting the `web.search_backend` field, the
        v1 default, and the architecture (tool name
        stable; backend is config). Cross-reference the
        `web_providers/` directory as the seam for
        future providers.
- [x] 2d. Add a "Web search" section to
        `docs/quickstart.md` between the skills section
        and the resilience checks. Two-paragraph
        operator note: out-of-the-box DuckDuckGo via
        `ddgs`; how to test from Telegram; how to
        switch backends later.
- [x] 2e. Run lint + mypy + pytest pass in `gateway/`
        and `telegram-bot/`. All green.
- [ ] 2f. Commit. Suggested message:
        `Phase 4.11/2: property tests, README, smoke`.

## Verification

- [ ] 3a. On the NAS, pull the latest, rebuild the
        gateway image (`docker compose down && docker
        compose up -d --build`). The image rebuild is
        required to pick up the new `ddgs` dep.
- [ ] 3b. `docker compose logs fitt-gateway 2>&1 |
        grep web_search` should show the tool
        registered with `active backend: ddgs` in
        the description, and `web_providers.ddgs`
        discovered at boot.
- [ ] 3c. Send a Telegram message: "what's the latest
        version of Python?". Confirm the agent calls
        `web_search`, returns a structured answer
        with URLs to python.org or similar
        authoritative sources, and the gateway log
        shows one
        `event="web_search.completed"` line with
        `backend=ddgs`, a numeric `latency_ms`, and
        a non-zero `result_count`.
- [ ] 3d. Send: "what time is it in Tokyo?". Confirm
        the agent uses `web_search` (or `http_get` if
        it picks a known time API). Either is fine
        for the smoke test — the goal is to confirm
        no transient DDG failures and reasonable
        latency.
- [ ] 3e. Set an invalid backend in
        `~/.fitt/config.yaml`
        (`web.search_backend: not-a-thing`),
        restart, send a Telegram query that
        triggers `web_search`. Confirm the agent
        gets the
        `Configured web search backend 'not-a-thing'
        is not registered. Available: ddgs.` error
        and reports it cleanly. Restore the config
        afterwards.

## Deferred — see design.md "Future Extensions"

These have spec coverage but no tasks here. Each is a
clean drop-in on top of the work above.

- **Additional providers** (SearXNG, Brave-free, Exa,
  Firecrawl, Tavily). Each is a single
  `web_providers/<name>.py` file plus a doc-side note.
  Half a day each. Add when daily-use friction calls
  for it.
- **`web_extract` tool**. Reads URLs and returns text
  content. The ABC's `supports_extract()` flag is
  already in place. ~half day for the dispatcher; the
  underlying providers (Tavily / Firecrawl) need to
  ship for content to differ from `http_get`.
- **`web_crawl` tool**. Niche; defer until a real use
  case surfaces.
- **Result-content wrapping for prompt-injection
  defense**. OpenClaw's `wrapWebContent` pattern. Phase
  when prompt-injection hardening becomes its own
  phase.
- **Per-session rate limiting on `web_search`**. When
  audit log shows a runaway agent making hundreds of
  search calls. Today's posture is "audit log catches
  it after the fact."
