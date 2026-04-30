# Design: FITT Phase 3 — Telegram + Browser Interface

## Architecture

Phase 3 adds two thin client services that both talk to the gateway
via its OpenAI-compatible endpoint. Neither service has its own
auth, routing, memory, or cost logic; all of that stays in the
gateway.

```
    ┌─────────────────┐          ┌─────────────────┐
    │  Telegram       │          │  Any browser    │
    │  (phone, watch) │          │  on Tailscale   │
    └────────┬────────┘          └────────┬────────┘
             │                            │
             │ Bot API                    │ HTTP
             ▼                            ▼
    ┌─────────────────┐          ┌─────────────────┐
    │ telegram-bot/   │          │ Open WebUI      │
    │ python service  │          │ Docker container│
    │ on Hub          │          │ on Hub :3000    │
    └────────┬────────┘          └────────┬────────┘
             │                            │
             │ POST /v1/chat/completions  │
             │ Authorization: Bearer <..> │
             ▼                            ▼
    ┌─────────────────────────────────────────────────┐
    │          FITT Gateway (Phase 1) on :8080         │
    │   - auth, routing, memory, sessions, logging     │
    └─────────────────────────────────────────────────┘
```

Both clients share one design commitment: **they never bypass the
gateway.** Telegram does not call OpenRouter directly; Open WebUI
does not talk to Ollama directly. Everything flows through the
gateway so memory/cost/logging is consistent across interfaces.

## Key Design Decisions

### Decision 1: Two separate services, not one

The Telegram bot is Python (`python-telegram-bot`) running as a
Windows service. Open WebUI is a Docker container. Running them as
separate units keeps each simple and lets us uninstall one without
touching the other. Both are optional; the gateway works alone.

### Decision 2: Bot calls the gateway over HTTP localhost

The bot process lives on the Hub. It calls
`http://127.0.0.1:8080/v1/chat/completions` with the Bearer token
from `secrets.yaml`. No new wire format, no sidecar, nothing.

### Decision 3: Open WebUI via Docker compose

Open WebUI is a substantial JS/Python stack. Packaging it as a
Windows service is painful; Docker is its native distribution.
Docker Desktop is already optional on the Hub (Phase 1 uses it for
nothing); Phase 3 makes it a hard dependency for Open WebUI
specifically.

### Decision 4: Session registry access via the library, not the HTTP gateway

The bot needs to create sessions on `/session new`. It could POST
to a dedicated gateway endpoint, but (a) we don't have such an
endpoint and (b) the bot runs on the same machine, so it can just
import `gateway.sessions.SessionRegistry` and manipulate the same
files the gateway reads. This avoids a new API surface and keeps
the gateway smaller.

The tradeoff: the bot and gateway must be deployed together and
point at the same `sessions_dir`. Documented in the install script.

### Decision 5: Streaming reply via message edit

Telegram has no native SSE. The conventional pattern is: send a
placeholder message ("..."), then `edit_message_text` every N ms
with the accumulated partial reply. We rate-limit edits to once per
~800ms to stay under Telegram's API limits (30 msgs/sec global, 1
msg/sec per chat for regular bots).

### Decision 6: Open WebUI signup disabled after first admin

Without this, anyone on the tailnet who browses to `:3000` would
see the signup page and could create an account that hits the
gateway (which trusts the Bearer token Open WebUI holds). We set
`ENABLE_SIGNUP=false` after the admin account is created.

## Module Design

### `telegram-bot/` directory (new at repo root)

```
telegram-bot/
├── pyproject.toml              # separate Python package
├── uv.lock
├── README.md
├── src/
│   └── fitt_telegram_bot/
│       ├── __init__.py
│       ├── __main__.py          # `python -m fitt_telegram_bot`
│       ├── bot.py               # main app object + lifecycle
│       ├── gateway_client.py    # async httpx wrapper around the gateway
│       ├── prefs.py             # per-chat preference store (JSON)
│       ├── handlers.py          # message / command handlers
│       ├── streaming.py         # "edit message as tokens arrive" helper
│       └── config.py            # load from ~/.fitt/ secrets.yaml + config.yaml
└── tests/
    ├── conftest.py
    ├── test_prefs.py
    ├── test_gateway_client.py
    ├── test_handlers.py
    └── test_streaming.py
```

### `telegram-bot/src/fitt_telegram_bot/config.py`

Reads the same `~/.fitt/config.yaml` + `~/.fitt/secrets.yaml` the
gateway uses. Specifically:

- `secrets.telegram.bot_token` - required, bot won't start without it.
- `secrets.telegram.allowlist_user_ids` - required (empty list is
  explicit "lock everyone out").
- `server.host` + `server.port` - where to POST to.
- The `personal` Bearer token from `allowed_tokens`.

### `gateway_client.py`

```python
class GatewayClient:
    async def chat(
        self,
        messages: list[dict],
        *,
        alias: str,
        session_id: str,
    ) -> AsyncIterator[str]:
        """Yield content deltas from the gateway's SSE stream."""

    async def list_aliases(self) -> list[str]:
        """GET /v1/models to populate /model picker."""
```

Always uses `stream=true` so we can edit-in-place. Errors surface as
a single yielded `"⚠️ <error>"` string rather than raising, so the
caller can just render to Telegram.

### `prefs.py`

```python
@dataclass
class ChatPrefs:
    chat_id: int
    alias: str = "fitt-default"
    session_id: str = "main"


class PrefsStore:
    def get(self, chat_id: int) -> ChatPrefs: ...
    def set_alias(self, chat_id: int, alias: str) -> None: ...
    def set_session(self, chat_id: int, session_id: str) -> None: ...
```

JSON at `~/.fitt/telegram/prefs.json`, atomic writes. Missing
entries fall back to defaults.

### `handlers.py`

python-telegram-bot-style handlers:

```python
async def text_message(update, context): ...   # main chat path
async def photo_message(update, context): ...  # multimodal
async def voice_message(update, context): ...  # stub reply
async def session_command(update, context): ...
async def model_command(update, context): ...
async def start_command(update, context): ...  # greet + show current prefs
async def help_command(update, context): ...
```

Each wraps an allowlist check. Non-allowlisted messages are
dropped silently.

### `streaming.py`

```python
class StreamingEditor:
    """Accumulate deltas, edit the Telegram message at most every
    ~800ms. Flush on stream completion."""

    def __init__(self, bot, chat_id: int, placeholder_message_id: int): ...
    async def append(self, delta: str) -> None: ...
    async def finalize(self) -> None: ...
```

### `bot.py`

Minimal: build the python-telegram-bot `Application`, register
handlers, set up graceful shutdown, call `run_polling()` (no
webhooks in v0 - we don't have a public HTTPS endpoint).

### Docker compose: Open WebUI

`docker-compose.yml` at the repo root:

```yaml
version: "3.9"
services:
  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    container_name: fitt-open-webui
    restart: unless-stopped
    ports:
      # Bound to 0.0.0.0 but restricted by Windows firewall to
      # Tailscale. Same pattern as the gateway.
      - "3000:8080"
    environment:
      # Open WebUI treats the gateway as a stock OpenAI-compatible
      # upstream.
      - OPENAI_API_BASE_URL=http://host.docker.internal:8080/v1
      - OPENAI_API_KEY=${FITT_BEARER_TOKEN}
      # Disable external fetches and feature flags we don't want.
      - ENABLE_SIGNUP=false
      - ENABLE_COMMUNITY_SHARING=false
      - ENABLE_OLLAMA_API=false
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - ./open-webui-data:/app/backend/data
```

The install script sets `FITT_BEARER_TOKEN` from `secrets.yaml` and
adds a `localhost:3000` firewall rule scoped to the Private profile.

### Install script additions

`scripts/install-telegram-bot.ps1` (new): registers the bot as a
second NSSM service, `FITTTelegramBot`. Verifies uv-managed venv in
`telegram-bot/.venv` exists (or runs `uv sync` with `-SetupVenv`).

`scripts/install-open-webui.ps1` (new): runs `docker compose up -d`
from the repo root, adds the firewall rule.

## Configuration additions

No changes to gateway `config.yaml`. Bot-specific settings live at
the bot process level (defaults + per-chat prefs file).

`secrets.yaml` already has the `telegram:` block reserved in Phase 1.
Phase 3 just starts reading it:

```yaml
telegram:
  bot_token: 123456:ABC-xxxxxxxxxxxxx
  allowlist_user_ids:
    - 123456789
```

## Failure handling

| Scenario                               | Behavior                                          |
|----------------------------------------|---------------------------------------------------|
| Gateway 5xx                            | Bot replies with "⚠️ gateway error: <msg>"; does not retry. |
| Gateway 503 + Retry-After              | Bot replies with "⚠️ rate limited, retry in Ns".  |
| Gateway unreachable (connection refused) | Bot replies with "⚠️ gateway down; is the service running?". |
| Invalid Bearer (401)                   | Bot logs fatal error, replies with "⚠️ misconfigured; check bot logs"; does not restart. |
| Telegram API rate-limited              | Back off + retry per python-telegram-bot's built-in handling. |
| Non-allowlisted user                   | Silent drop + one-line log.                      |
| Unknown `/session <id>`                | Reply with valid ids list.                        |
| Unknown `/model <alias>`               | Reply with valid aliases list.                    |
| Corrupted prefs.json                   | Log warning, reset in-memory state to defaults, leave file as-is. |
| Voice message (Phase 3 stub)           | Reply with "Voice is not wired up yet."          |

## Correctness Properties

### Property 1: Allowlist enforcement

*For any* Telegram update whose `effective_user.id` is not in the
allowlist, the bot makes no HTTP call to the gateway and sends no
reply to Telegram.

**Validates: 2.1, 2.2, 2.3**

### Property 2: Gateway as sole upstream

*For any* path in the bot code that produces an LLM response, the
code reaches it via `GatewayClient`. No direct calls to OpenRouter,
Anthropic, or Ollama.

**Validates: architectural commitment.**

### Property 3: Prefs persistence

*For any* preference change via `/session` or `/model`, the change
is visible after the bot process restarts and is equal to what was
last set.

**Validates: 3.4, 4.3, 8.1, 8.2**

### Property 4: Session / alias validation

*For any* `/session <id>` where `<id>` is not in the registry's
`valid_ids()`, the prefs file is not updated; the bot replies with
the valid set.

**Validates: 3.2, 4.2**

## Testing Strategy

### Unit tests

- `test_prefs_empty_file_uses_defaults`
- `test_prefs_set_alias_persists`
- `test_prefs_set_session_persists`
- `test_prefs_corrupted_json_logs_and_defaults`
- `test_prefs_atomic_write` (interrupt simulation)
- `test_gateway_client_chat_streams_deltas` (respx-mocked gateway)
- `test_gateway_client_rate_limited_surface` (503 with retry-after)
- `test_gateway_client_unreachable_yields_error_string`

### Handler tests (python-telegram-bot provides Application.bot
mocks)

- `test_allowlist_drops_non_allowlisted_user`
- `test_allowlist_accepts_allowlisted_user`
- `test_text_message_forwards_to_gateway_with_current_prefs`
- `test_session_list_includes_current_marker`
- `test_session_switch_updates_prefs`
- `test_session_switch_unknown_id_lists_valid_ids`
- `test_model_list_includes_current_marker`
- `test_model_switch_unknown_alias_lists_valid_aliases`
- `test_voice_message_returns_stub_reply`
- `test_photo_forwards_as_multimodal`

### Streaming tests

- `test_streaming_editor_accumulates_and_flushes`
- `test_streaming_editor_rate_limits_edits`
- `test_streaming_editor_finalizes_on_empty_content`

### Integration tests

- `test_end_to_end_text_message_with_mocked_gateway_and_telegram`
- `test_prefs_survive_simulated_restart`

### Live-only tests (at-home, not in CI)

- Actual `/start` from the user's phone produces the welcome
  message.
- Actual text message round-trips through the live gateway.
- Actual photo (screenshot of an error) gets a text description.
- `/model fitt-smart` routes to OpenRouter end to end.

## Known concerns

- **No webhook support in v0.** We use `run_polling()`, which needs
  outbound internet but no public IP. Fine for personal use.
- **Long-poll timeouts during a gateway stream.** If the gateway
  stalls, python-telegram-bot might reset the connection. Known
  issue; mitigation in Phase 10 if it actually bites.
- **Open WebUI sessions aren't mapped to FITT sessions.** Open
  WebUI has its own chat concept with its own id. v0 accepts this;
  every Open WebUI chat just uses `main`. Phase 3.5 (if we ever
  need it) could add an `X-FITT-Session` header injector.

## Future extensions

- Phase 3.5: webhook mode for bots behind a reverse proxy.
- Phase 4: tool-approval UI via Telegram inline keyboards.
- Phase 8: voice round-trip (inbound voice note → STT → gateway →
  TTS → outbound voice note).
