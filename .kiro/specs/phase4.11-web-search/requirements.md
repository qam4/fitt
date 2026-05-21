# Requirements Document

Phase 4.11 — Web Search Tool.

## Introduction

This phase adds a `web_search` tool to the FITT gateway. The
agent calls `web_search(query, limit?)` and receives a structured
list of `{title, url, snippet}` results from a configurable
provider. v1 ships one provider (DuckDuckGo via the maintained
`ddgs` PyPI package, no API key required); the architecture
leaves room for SearXNG, Brave-free, and others as drop-in
additions later.

This is the "default-on web search" upgrade flagged in the
OpenClaw and Hermes audits as the largest day-1 UX delta. With
it, "what's the latest version of X / what happened with Y / how
do I do Z in tool Q" stops bouncing the user out of FITT to a
browser.

The shape mirrors the convergent pattern from both reference
systems: **one tool name, multiple provider implementations
behind an ABC, operator picks a default in `config.yaml`.**

The phase is explicitly v0 scope and does NOT include:

- Multiple bundled providers (only `ddgs` ships in v1; the ABC
  is in place so SearXNG / Brave-free can be added without
  refactoring).
- A `web_extract` or `web_crawl` tool. Those are separate
  capabilities; the ABC declares them with `supports_*` flags
  but the registry only exposes `web_search` in v1.
- Per-query backend override. The agent always uses the
  configured default. Operators who want to switch backends
  edit `config.yaml` and restart.
- Search-result caching. Each call hits the upstream live.
- Rate-limit handling beyond what `ddgs` already does
  internally. If DuckDuckGo rate-limits us, we surface the
  error and the agent decides whether to retry.

## Glossary

- **Web_Search_Tool**: The single tool registered with FITT's
  ToolRegistry under the name `web_search`. Always-on; the
  agent calls it the same way regardless of configured backend.
- **WebSearchProvider**: ABC for backend implementations. One
  required method (`search`) plus an `is_available()` probe and
  a `name` property. Each provider lives under
  `gateway/src/gateway/tools/web_providers/<name>.py`.
- **DDGS_Provider**: The default `ddgs` implementation, a thin
  wrapper around the [`ddgs`](https://pypi.org/project/ddgs/)
  PyPI package. No API key required.
- **SearchResult**: One result entry — a dict with required
  keys `title` (str), `url` (str), `snippet` (str), and
  optional `position` (int).
- **Web_Search_Config**: The `web:` block in
  `~/.fitt/config.yaml`. Today carries one key,
  `search_backend`, naming the active provider.
- **Web_Search_Registry**: Module-level dict mapping provider
  name → instance, populated at gateway boot from the
  available providers. The dispatcher tool looks up the
  configured backend here.

## Requirements

### Requirement 1: Register the `web_search` tool

**User Story:** As the agent, I want a single `web_search` tool
that I can call regardless of which backend is configured, so
my tool-call code path doesn't change when the operator swaps
providers.

#### Acceptance Criteria

1. WHEN the gateway builds its ToolRegistry, THE
   Web_Search_Tool SHALL register exactly one tool named
   `web_search` with `requires_project=False`,
   `default_bucket=AUTO`, and `kind="inline"`.
2. THE tool SHALL accept exactly two arguments: `query`
   (string, required, 1-500 codepoints after stripping
   whitespace) and `limit` (integer, optional, default 5,
   clamped to the inclusive range [1, 20]).
3. THE tool SHALL reject calls with a missing or empty
   `query` argument with a structured `ToolResult.error`
   naming the offending field.
4. THE tool SHALL reject `limit` values that are not integers
   or fall outside [1, 20] with a structured
   `ToolResult.error` naming the bound that was violated.
5. WHEN the tool is called with valid arguments, THE tool
   SHALL look up the configured backend via the
   Web_Search_Registry and dispatch the call to that
   provider's `search()` method.
6. WHEN the configured backend is not present in the
   Web_Search_Registry, THE tool SHALL return a structured
   error of the form
   `"Configured web search backend '<name>' is not registered.
   Available: <comma-separated list>."` and SHALL NOT raise.

### Requirement 2: WebSearchProvider ABC

**User Story:** As a future contributor (or as me, when I
eventually want a SearXNG provider), I want a clear contract
for what a backend must implement, so a new provider is just a
file under `web_providers/`.

#### Acceptance Criteria

1. THE `WebSearchProvider` ABC SHALL declare an abstract
   `name` property returning the provider's stable
   identifier string used in `web.search_backend` config
   keys.
2. THE ABC SHALL declare an abstract `search(query: str,
   limit: int) -> dict` method returning either
   `{"success": True, "data": {"web": [SearchResult, ...]}}`
   on success or `{"success": False, "error": <str>}` on
   failure.
3. THE ABC SHALL declare a non-abstract `is_available() ->
   bool` method that providers override to advertise
   availability cheaply (e.g., is the underlying SDK
   importable, are required env vars present). The base
   implementation SHALL return `True`.
4. THE `is_available()` method SHALL NOT perform network I/O.
   It is called at tool-registration time and on every
   ``list_capabilities`` call; a network probe per call would
   be a measurable cold-path tax.
5. THE ABC SHALL declare non-abstract `supports_search() ->
   bool`, `supports_extract() -> bool`, and `supports_crawl()
   -> bool` capability flags. The base implementations SHALL
   return `True` for `supports_search` and `False` for the
   other two; concrete providers override as needed. Today
   only `supports_search()` is consulted by the registry; the
   other two are forward-compat for Phase 4.12+ when a
   `web_extract` / `web_crawl` tool may ship.
6. WHEN a SearchResult is included in a successful response,
   THE result SHALL contain at minimum the keys `title`
   (string), `url` (string), and `snippet` (string).
   Providers MAY include `position` (1-based integer) and
   provider-specific metadata fields; consumers SHALL ignore
   unknown keys.

### Requirement 3: DDGS provider

**User Story:** As an operator with no API keys, I want web
search to work out of the box using DuckDuckGo, so I don't
need to register an account, manage a key, or run a SearXNG
container before I can use the feature.

#### Acceptance Criteria

1. THE DDGS_Provider class SHALL inherit from
   `WebSearchProvider` and SHALL set `name` to the literal
   string `"ddgs"`.
2. THE DDGS_Provider's `is_available()` method SHALL return
   `True` if and only if the `ddgs` Python package is
   importable in the gateway process. It SHALL NOT raise.
3. WHEN `search(query, limit)` is called, THE DDGS_Provider
   SHALL call `ddgs.DDGS().text(query, max_results=limit)`
   and SHALL return at most `limit` SearchResults, each
   carrying `title` and `url` and `snippet` derived from the
   ddgs response fields (`title`, `href`, `body`).
4. IF the `ddgs` package is not importable at the moment of
   the call, THEN the DDGS_Provider SHALL return
   `{"success": False, "error": "ddgs package is not
   installed — run `uv add ddgs` in the gateway package."}`
   without raising.
5. IF `ddgs` raises any exception during the call, THEN the
   DDGS_Provider SHALL catch it and return
   `{"success": False, "error": f"DuckDuckGo search failed:
   {exc}"}` with the truncated exception message (max 240
   chars). The provider SHALL log the failure at WARNING
   level with the structured field
   `event="web_search.provider_failed"`, the provider name,
   the exception class, and the (full) exception message.
6. THE DDGS_Provider SHALL be the default provider — when no
   `web.search_backend` config key is present, the gateway
   SHALL behave as if `web.search_backend: "ddgs"` were
   configured.

### Requirement 4: Configuration

**User Story:** As an operator, I want to choose which web
search backend the gateway uses without recompiling or editing
code, so swapping DDGS for a future SearXNG instance is a
config-only change.

#### Acceptance Criteria

1. THE gateway SHALL accept a top-level `web:` block in
   `config.yaml` containing the optional string field
   `search_backend`. Both an absent block and an absent
   `search_backend` key SHALL be treated as equivalent and
   default to `"ddgs"`.
2. IF `web.search_backend` is present and is not a string,
   THEN the gateway SHALL fail to start, emit one ERROR log
   line naming the field and the received YAML type, and
   exit with a non-zero status code.
3. WHEN the configured `web.search_backend` value names a
   provider that is not registered in the
   Web_Search_Registry at boot, THE gateway SHALL log a
   WARNING line at boot naming the missing provider and
   continue starting (so a misconfigured backend doesn't
   block FITT). Subsequent `web_search` calls SHALL surface
   the structured error per Requirement 1.6.
4. THE `configs/config.example.yaml` file SHALL document the
   `web.search_backend` field with placeholder value `"ddgs"`
   and exactly one comment sentence explaining its purpose.

### Requirement 5: Capability-block exposure

**User Story:** As the agent, when I look at the
`[Capabilities]` block in my system prompt, I want
`web_search` to appear so I know it's available, so I can call
it for fresh-information questions instead of guessing.

#### Acceptance Criteria

1. WHEN the gateway builds the `[Capabilities]` block, THE
   `web_search` tool SHALL appear in the listed-tools
   section with its description visible.
2. THE `web_search` tool's description SHALL include the
   active backend's name. Example:
   `Search the web (active backend: ddgs).`. Operator-side
   visibility into which backend is currently serving search
   helps both the agent's reasoning and the operator's
   debugging without shipping a separate
   `web_search_status` tool.

### Requirement 6: End-to-end integration test

**User Story:** As a developer, I want one integration test
that proves "agent calls `web_search` → DDGS provider → mocked
upstream returns canned results → tool returns structured
JSON," so the dispatcher path is pinned without making real
network calls.

#### Acceptance Criteria

1. THE test suite SHALL include one integration test that
   constructs a Config with `web.search_backend: "ddgs"`,
   builds the gateway via `create_app(config)`, registers
   the `web_search` tool, monkey-patches the `ddgs.DDGS`
   class with a stub returning a fixed list of three
   results, and asserts the tool's response is
   `{"success": True, "data": {"web": [...3 results...]}}`
   with the expected `title`, `url`, and `snippet` for each.
2. THE test SHALL run under `uv run pytest` without any
   real network I/O. The `ddgs` import is allowed (so the
   provider's `is_available()` returns truthfully on CI);
   only the upstream HTTP call SHALL be stubbed.
3. THE test SHALL also exercise the failure path: with the
   `ddgs.DDGS` stub raising `RuntimeError("rate limited")`,
   the tool SHALL return
   `{"success": False, "error": "<message containing 'rate limited'>"}`
   and the gateway logs SHALL contain one
   `event="web_search.provider_failed"` WARNING line.

### Requirement 7: Failure isolation

**User Story:** As an operator, I want a misconfigured or
broken web search backend to never block the rest of the
gateway, so a transient DDGS rate limit doesn't take down the
chat path.

#### Acceptance Criteria

1. THE Web_Search_Tool SHALL catch every exception raised
   inside the provider's `search()` method, wrap it in a
   structured error result, and SHALL NOT propagate the
   exception to the agent loop or the chat handler.
2. THE `web_search` tool's failure path SHALL NOT surface
   the upstream provider's stack trace to the agent. Only
   the exception class name and a single-line message
   (truncated to 240 chars) SHALL appear in the
   `error` field.
3. WHEN the gateway boots without the `ddgs` package
   importable AND `web.search_backend` resolves to `"ddgs"`,
   THE gateway SHALL still start successfully. The
   Web_Search_Registry SHALL include the `ddgs` provider
   with `is_available() == False`; the dispatcher SHALL
   surface the "package not installed" error per
   Requirement 3.4 on the first call.

### Requirement 8: Operator visibility

**User Story:** As an operator, I want gateway logs to show
which backend served a `web_search` call and how long it
took, so I can spot regressions or rate-limit cliffs without
digging through stack traces.

#### Acceptance Criteria

1. WHEN the Web_Search_Tool dispatches a call to a provider
   AND the provider returns a successful response, THE tool
   SHALL log one INFO line with the structured fields
   `event="web_search.completed"`, `backend` (string),
   `query_chars` (non-negative integer = the codepoint
   length of the query), `result_count` (non-negative
   integer), and `latency_ms` (non-negative integer).
2. WHEN the Web_Search_Tool dispatches a call AND the
   provider returns `{"success": False, ...}`, THE tool
   SHALL log one WARNING line with
   `event="web_search.failed"`, `backend`, `query_chars`,
   `latency_ms`, and `error` (truncated to 240 chars).
3. THE log lines SHALL NOT include the raw query string.
   Query content can carry user-private context (PII, work
   secrets, etc.); the codepoint length and result count
   are sufficient for operational telemetry without leaking
   content.
