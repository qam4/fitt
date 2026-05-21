# Design: FITT Phase 4.11 — Web Search Tool

## Overview

Phase 4.11 adds a single `web_search` tool to FITT's tool
registry, backed by a pluggable provider layer. The agent
calls `web_search(query, limit=5)` and gets back a structured
list of `{title, url, snippet}` results. The default and only
provider in v1 is DuckDuckGo (via the `ddgs` PyPI package, no
API key); the architecture leaves seams for SearXNG,
Brave-free, Exa, etc. as drop-in additions later.

The design mirrors the convergent OpenClaw/Hermes pattern:
**one tool name, multiple provider implementations behind an
ABC, operator picks the active provider in `config.yaml`**.
Both reference systems went this way because (a) most "web
search" code is provider-specific HTTP and parsing, and (b)
operators with different infrastructure preferences want
different backends without changing the agent's tool surface.

This is the most-cited UX upgrade from both audits — the
single biggest day-1 unlock. With it, FITT can answer "what's
the latest version of X / what happened with Y / how do I do
Z" without bouncing the user out to a browser.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Gateway boot                                               │
│                                                             │
│   Config(... web=WebSearchConfig(backend="ddgs"))           │
│                            │                                │
│                            ▼                                │
│   web_providers/__init__.py auto-registers each provider    │
│   via discover_providers() into Web_Search_Registry         │
│                            │                                │
│                            ▼                                │
│   build_inline_tools(...) registers `web_search`            │
│   pointing at the dispatcher                                │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  Per agent call                                             │
│                                                             │
│   web_search(query="latest python", limit=5)                │
│              │                                              │
│              ▼                                              │
│   _web_search_dispatch(args, ctx)                           │
│      ├─ validate args                                       │
│      ├─ look up Web_Search_Registry[config.web.backend]     │
│      └─ call provider.search(query, limit)                  │
│                            │                                │
│                            ▼                                │
│   DDGSWebSearchProvider.search()                            │
│      └─ ddgs.DDGS().text(query, max_results=limit)          │
│                            │                                │
│                            ▼                                │
│   {"success": True, "data": {"web": [{title, url, ...}]}}   │
└─────────────────────────────────────────────────────────────┘
```

### Key Design Decisions

#### Decision 1: Use `ddgs` PyPI package, not hand-rolled HTML scraping

OpenClaw scrapes DuckDuckGo's HTML pages with a regex parser
(~30 lines). Hermes uses the `ddgs` package (~50KB, pure
Python, well-maintained). Both work; the `ddgs` choice trades
"one more dependency" for "we don't own the parser when DDG
changes their HTML."

FITT picks `ddgs`:
- Principle 3 (use mature tools).
- DDG's HTML changes occasionally and silently; a
  hand-rolled scraper rots without us noticing until a real
  user query returns zero results.
- The `ddgs` package handles cooldown/rate-limit hints,
  region/safesearch params, and other DDG quirks we'd otherwise
  re-discover.
- Single dependency, well-pinned: `ddgs>=9.0,<10`.

If `ddgs` ever stops being maintained or makes breaking
changes, switching to hand-rolled scraping is a 30-line PR
inside the same provider module.

#### Decision 2: Provider ABC instead of monolithic dispatcher

A YAGNI argument says "ship one DDG-only `web_search` tool;
add multi-provider when you need it." But:

- The OpenClaw and Hermes audits both confirmed every
  serious self-hosted assistant ends up with a multi-provider
  layer eventually. SearXNG (no scrape brittleness, on the
  Hub) is a likely Phase 7+ addition; Brave-free or Tavily
  are reasonable when API quota matters.
- The ABC adds one file (~80 lines including the docstring)
  and one tiny registry init (~30 lines). It's not a heavy
  abstraction.
- Refactoring a monolithic implementation later is the same
  work as designing it pluggable now, plus a migration
  step.

The ABC is the right shape from day one. We just ship one
provider initially; that's the simplification.

#### Decision 3: Result schema mirrors OpenClaw's wrapped-content layout

Both reference systems normalize to a list of
`{title, url, snippet}` records at the boundary. They
sometimes wrap result fields in tags like
`<wrapped:web_search>` for prompt-injection defense
(OpenClaw does this; Hermes doesn't).

FITT's v1 doesn't do the wrapping — we'd need a corresponding
unwrap on the agent side to make the model do anything useful
with the result, and we'd have to handcraft the wrap-tag
contract. Hermes ships unwrapped; their docs note the
prompt-injection risk but accept it. We follow Hermes here.

A future hardening pass could add wrap/unwrap. Not this
phase.

#### Decision 4: One tool, two capabilities deferred (`web_extract`, `web_crawl`)

Hermes ships three companion tools: `web_search` (find
URLs), `web_extract` (read URLs), `web_crawl` (walk a seed
URL). The ABC has capability flags for all three.

FITT's v1 ships only `web_search`. The ABC carries the same
three flags so providers can advertise extract/crawl support,
but the registry only exposes the search-capable ones today.

`web_extract` is straightforward — agent already has
`http_get` for the trivial case, and Tavily/Firecrawl
extract is a Phase 4.12 add. `web_crawl` is more niche.
Neither is on the critical path now.

#### Decision 5: Description carries the active backend name

The `[Capabilities]` block normally renders each tool with
its static description string. For `web_search`, the
description is built dynamically at registration time:
`"Search the web for fresh info (active backend: ddgs)."`

Two reasons:
- The model gets a small signal about *which* backend is
  serving so it can adapt phrasing if a backend has known
  quirks (DDG often returns Wikipedia-heavy results; Brave
  doesn't).
- The operator inspecting the system prompt with
  `log_bodies: true` immediately sees what's serving search
  without checking config.

The dynamic-description path requires `build_inline_tools()`
to read `config.web.search_backend` at registration time
(once per gateway boot). Stable across the process lifetime
— matches the Phase 4.10 prompt-cache-stability decision.

#### Decision 6: Don't log query content; do log result count and latency

Operator visibility (Requirement 8) needs enough signal to
spot regressions ("DDG started returning zero results
yesterday") without leaking user-private context. We log
`query_chars` (codepoint length), `result_count`, and
`latency_ms` per call.

The query string itself can carry PII, work secrets, search
embarrassment — content the operator's audit log shouldn't
record by default. The chat handler's existing `log_bodies`
toggle covers full-content logging when explicitly enabled
for debugging.

## Components and Interfaces

### Component 1: `gateway/src/gateway/tools/web_search.py` — dispatcher tool

Single async tool function plus the `Tool` factory exported
into `build_inline_tools()`.

```python
async def _tool_web_search(
    args: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    """Dispatch a web search via the configured provider.

    Validates query and limit, looks up the active provider in
    the Web_Search_Registry, dispatches, logs, returns a
    structured ToolResult. Catches every provider-side
    exception per Requirement 7.1.
    """
```

The factory function (`build_web_search_tool(config)`) reads
`config.web.search_backend` at gateway-boot time and builds
the tool with a description that names the active backend per
Decision 5.

### Component 2: `gateway/src/gateway/tools/web_providers/__init__.py` — ABC + registry

```python
class WebSearchProvider(abc.ABC):
    """Plugin-facing ABC. v1 uses one method; v0+1 future-
    compatible flags for extract/crawl."""

    @property
    @abc.abstractmethod
    def name(self) -> str: ...

    def is_available(self) -> bool:
        """Cheap check; no network I/O."""
        return True

    def supports_search(self) -> bool: return True
    def supports_extract(self) -> bool: return False
    def supports_crawl(self) -> bool: return False

    @abc.abstractmethod
    def search(self, query: str, limit: int) -> dict[str, Any]:
        ...


# Module-level registry populated at import time.
WEB_SEARCH_REGISTRY: dict[str, WebSearchProvider] = {}


def register_provider(provider: WebSearchProvider) -> None:
    WEB_SEARCH_REGISTRY[provider.name] = provider


def discover_providers() -> None:
    """Import every concrete provider module so each calls
    register_provider() at import time. Idempotent; called
    once at boot."""
```

### Component 3: `gateway/src/gateway/tools/web_providers/ddgs.py` — DDGS provider

```python
class DDGSWebSearchProvider(WebSearchProvider):
    @property
    def name(self) -> str:
        return "ddgs"

    def is_available(self) -> bool:
        try:
            import ddgs  # noqa: F401
            return True
        except ImportError:
            return False

    def search(self, query: str, limit: int) -> dict[str, Any]:
        # Try-import inside; surface "package not installed"
        # as a structured error per Requirement 3.4.
        ...


register_provider(DDGSWebSearchProvider())
```

### Component 4: `gateway/src/gateway/config.py` — WebSearchConfig

```python
class WebSearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search_backend: str = "ddgs"


class Config(BaseModel):
    # ... existing fields ...
    web: WebSearchConfig = Field(default_factory=WebSearchConfig)
```

Per Requirement 4.2, Pydantic's standard ValidationError on
non-string `search_backend` causes the gateway to exit
non-zero — same posture as `memory.skills_dir` in Phase 4.10.

### Component 5: `gateway/src/gateway/tools/__init__.py` — wire-up

`build_inline_tools(registry, config)` calls
`web_providers.discover_providers()` once and appends
`build_web_search_tool(config)` to its returned list.

Boot sequence:
1. `discover_providers()` imports `web_providers/ddgs.py`,
   which calls `register_provider(DDGSWebSearchProvider())`.
2. The `web_search` tool factory reads
   `config.web.search_backend`, looks up the registered
   provider for description text, builds the `Tool`.
3. Registry has both the `web_search` tool and the
   provider (via the dispatcher's lookup at call time).

### Component 6: `configs/config.example.yaml`

```yaml
# ------------------------------------------------------------------
# Web search (Phase 4.11)
# ------------------------------------------------------------------
# The agent can call web_search(query) to fetch fresh info from
# the web. The default backend is ddgs (DuckDuckGo via the ddgs
# Python package; no API key required). When future providers
# ship (SearXNG, Brave-free, Exa, ...), swap the backend name
# here without editing code.
web:
  search_backend: ddgs
```

### Component 7: `pyproject.toml` — dependency

Adds `ddgs>=9.0,<10` to gateway's `[project] dependencies`.
Pure Python, ~50KB, no transitive bloat. Pinned to a stable
range matching Hermes's choice.

## Data Models

### `WebSearchConfig`

```python
class WebSearchConfig(BaseModel):
    search_backend: str = "ddgs"
```

Default `"ddgs"`. Pydantic enforces string type.

### `SearchResult` (dict shape, not a class)

```python
{
    "title": "Python 3.13.5 release notes",
    "url": "https://www.python.org/downloads/release/python-3135/",
    "snippet": "Python 3.13.5 is the fifth maintenance release...",
    "position": 1,  # optional, 1-based
}
```

Required keys: `title`, `url`, `snippet`.
Optional keys: `position`, plus any provider-specific
metadata. Consumers ignore unknown keys.

### Tool response envelope

Successful call:
```python
{
    "success": True,
    "data": {
        "web": [
            {"title": ..., "url": ..., "snippet": ..., "position": 1},
            ...
        ],
    },
}
```

Failed call:
```python
{
    "success": False,
    "error": "<single-line message, truncated to 240 chars>",
}
```

The dispatcher wraps the provider's response in this envelope
verbatim — no transformation in v1.

## Error Handling

| Failure mode | Behavior | Requirement |
|---|---|---|
| Missing `query` argument | Tool returns structured `ToolResult.error` | 1.3 |
| `query` > 500 codepoints | Tool returns structured error naming the bound | 1.2 |
| `limit` not an int | Tool returns structured error | 1.4 |
| `limit` outside [1, 20] | Clamped to range; no error | 1.2 |
| Configured backend not registered | `{"success": False, "error": "Configured ... not registered. Available: ..."}` | 1.6 |
| Backend not available (`is_available() == False`) | Provider returns `{"success": False, "error": "<package> is not installed — run `uv add <package>`"}` per Requirement 3.4 | 3.4, 7.3 |
| `ddgs` raises an exception | Caught, logged at WARNING, returned as `{"success": False, "error": "DuckDuckGo search failed: <truncated message>"}` | 3.5, 7.1 |
| Non-string `web.search_backend` in config | Gateway exits non-zero with Pydantic ValidationError | 4.2 |
| Configured backend missing at boot | WARNING log; gateway starts; `web_search` calls fail per 1.6 | 4.3 |
| Provider response missing required `title`/`url`/`snippet` | Forwarded as-is in v1; hardening deferred (consumers SHOULD treat as best-effort) | — |

Per Requirement 7.1, exceptions never propagate out of the
dispatcher to the agent loop or chat handler.

## Tools and Dependencies

New runtime dependency: **`ddgs>=9.0,<10`**. Pure Python, no
binary compile, ~50KB. Pinned tight to match Hermes's
choice.

Other dependencies (already in the tree):
- `pydantic` — `WebSearchConfig` validation.
- `httpx` (transitively, via `ddgs`) — actual HTTP transport.
- Stdlib `abc`, `logging`, `time`.

Dev dependencies (no additions; existing test fixtures cover
the integration test).

## Security

- `web_search` is read-only and rate-limit-bound; goes in
  `default_bucket=AUTO`. No approval prompt per call;
  matches `http_get`'s existing posture.
- Provider responses are untrusted input. Today they go
  back to the model unwrapped (Decision 3); a malicious
  search result snippet could attempt prompt injection.
  This is the same risk class as `http_get` already has.
  Hardening deferred.
- `query` is forwarded to DDG verbatim. We don't log query
  content (Decision 6) so an audit-log leak doesn't expose
  the user's searches.
- The DDG endpoint resolves to a public IP; no Tailscale
  exposure or local-network probe risk.

## Correctness Properties

### Property 1: Successful dispatch shape

*For any* call to `web_search(query, limit)` where the
configured provider's `search()` returns
`{"success": True, "data": {"web": [results]}}`, the tool's
`ToolResult.payload` SHALL be a JSON-encoded copy of that
structure.

**Validates: Requirements 1.5, 2.2**

### Property 2: Failure isolation

*For any* exception raised by the provider's `search()`
method, the tool returns a structured failure result and
SHALL NOT propagate the exception out of the dispatcher.

**Validates: Requirements 7.1, 7.2**

### Property 3: Limit clamping

*For any* `limit` outside [1, 20] passed to `web_search`,
the tool either rejects the call (non-integer, missing) or
clamps to the range; the provider always receives an int in
[1, 20].

**Validates: Requirements 1.2, 1.4**

### Property 4: Logging discipline

*For any* completed `web_search` call (success or failure),
the gateway logs SHALL contain exactly one structured log
record with `event="web_search.completed"` (success) or
`event="web_search.failed"` (failure), and SHALL NOT
contain the raw query string in either record.

**Validates: Requirements 8.1, 8.2, 8.3**

## Testing Strategy

### Unit Tests for the ABC + registry (`tests/test_web_providers.py`)

- `test_register_provider_indexes_by_name`
- `test_discover_providers_imports_ddgs`
- `test_provider_abc_default_capability_flags` —
  `supports_search` defaults True; extract / crawl default
  False.
- `test_provider_abc_default_is_available_returns_true`

### Unit Tests for the DDGS provider (`tests/test_web_search_ddgs.py`)

- `test_ddgs_name_is_ddgs`
- `test_ddgs_is_available_when_package_installed`
- `test_ddgs_is_available_when_package_missing` (mocked
  `import` failure)
- `test_ddgs_search_returns_normalized_results` (DDGS class
  mocked to return canned hits)
- `test_ddgs_search_caps_results_at_limit`
- `test_ddgs_search_handles_runtime_error` (DDGS raises;
  provider returns success=False, error truncated to 240
  chars, exception class logged)
- `test_ddgs_search_handles_import_error_at_call_time`
  (import inside `search()` fails; provider returns
  success=False with install instruction)

### Unit Tests for the dispatcher (`tests/test_web_search.py`)

- `test_dispatcher_rejects_missing_query`
- `test_dispatcher_rejects_empty_query`
- `test_dispatcher_rejects_query_over_500_codepoints`
- `test_dispatcher_rejects_limit_not_int`
- `test_dispatcher_clamps_limit_below_1` (limit=0 → 1)
- `test_dispatcher_clamps_limit_above_20` (limit=100 → 20)
- `test_dispatcher_default_limit_is_5`
- `test_dispatcher_unknown_backend_returns_error_with_available_list`
- `test_dispatcher_logs_completed_event_on_success`
- `test_dispatcher_logs_failed_event_on_failure`
- `test_dispatcher_log_lines_do_not_contain_query`

### Integration test (`tests/test_web_search_e2e.py`)

Per Requirement 6, one integration test that:
1. Builds a Config with `web.search_backend: "ddgs"`.
2. Constructs the gateway via `create_app(config)`.
3. Monkey-patches `ddgs.DDGS` with a stub returning three
   canned `{"title", "href", "body"}` dicts.
4. Calls the registered `web_search` tool via the
   ToolRegistry (not via HTTP — same pattern as
   `test_tools_fileops.py`).
5. Asserts the response is
   `{"success": True, "data": {"web": [3 results]}}` with
   correct fields.
6. Repeats the call with `ddgs.DDGS` stubbed to raise
   `RuntimeError("rate limited")`; asserts
   `{"success": False, "error": <message containing "rate limited">}`.
7. Tagged `# Phase 4.11, Requirement 6`.

### Property tests (`tests/test_web_search_properties.py`, hypothesis)

- **Phase 4.11, Property 2: Failure isolation** — generate
  random Exception subclasses; confirm the dispatcher
  catches each and returns success=False without raising.
- **Phase 4.11, Property 3: Limit clamping** — generate
  random integer limits; confirm the limit forwarded to
  the provider is in [1, 20].

### Manual / smoke tests

After landing on the NAS:
1. Restart `fitt-gateway`.
2. In Telegram: "what's the latest version of Python?"
   Expect: agent calls `web_search`, returns top results
   with URLs to python.org.
3. In Telegram: "what time is it in Tokyo?"
   Expect: search-quality answer; if it picks up
   `worldtimeapi.org` or similar, that's the model
   reasoning over results — not a skill, just the tool
   working.

## Known Concerns (tracked, not blocking)

- **DDG rate limits.** DDG can throttle aggressive callers.
  In v1 we surface the error and let the agent decide
  whether to retry. If real-world use surfaces frequent
  rate limits, the next move is a SearXNG provider on the
  Hub.
- **DDG HTML changes.** `ddgs` package owns this; if it
  breaks, we update the dep. v1 doesn't snapshot results
  so a missed update silently degrades quality.
- **Result quality varies.** DDG often returns
  Wikipedia-heavy results; some queries the model
  already knew the answer to come back with worse
  results from search. The "trivial skills won't fire"
  lesson from Phase 4.10 applies: the agent shouldn't
  call `web_search` for things it already knows. The
  capability-block description is the only nudge in v1.
- **No usage metering.** A runaway agent could make
  hundreds of search calls per session. Today's posture
  is "audit log catches it after the fact." A per-session
  rate limit on `web_search` is a Phase 4.12+ concern.

## Future Extensions (explicit non-goals for Phase 4.11)

- Additional providers: SearXNG, Brave-free, Exa,
  Firecrawl, Tavily. Each is a drop-in
  `web_providers/<name>.py` file. Half a day each.
- `web_extract` tool. Reads URLs and returns text content.
  Tavily/Firecrawl handle this; the ABC is ready.
- `web_crawl` tool. Niche; defer until a real use case
  surfaces.
- Per-query backend override via `web_search(backend="...")`.
  Today's "operator picks one in config" is sufficient.
- Result-content wrapping for prompt-injection defense
  (OpenClaw's `wrapWebContent`). When prompt-injection
  hardening becomes a phase, this slots in.
- Per-session rate limiting on `web_search`.
- Search result caching (would need a cache backend; not
  worth the complexity until usage data shows it pays).
