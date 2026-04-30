# FITT Gateway

OpenAI-compatible HTTP gateway that routes chat-completion requests
between local Ollama models and cloud providers (OpenRouter primary,
Anthropic optional) based on an alias the client asks for.

- **Clients ask for aliases**, never concrete model IDs. The gateway
  resolves each alias to a model + optional fallback at request time.
- **Auth by Bearer token** on every `/v1/*` endpoint except discovery.
- **One daemon, many interfaces**: IDE (VS Code + Continue / Cursor /
  Kiro), Telegram (later), Open WebUI (later), curl.

## Install

Follow [`../docs/quickstart.md`](../docs/quickstart.md). One page,
10 steps, start to finish. This README is the *reference* for when
you already have the gateway running.

## Running locally during development

Requires [uv](https://docs.astral.sh/uv/) on PATH
(`winget install --id=astral-sh.uv -e`). See the quickstart for the
full setup.

```powershell
cd gateway
uv sync                     # first run: ~30 seconds; after: instant
uv run pytest               # run the test suite
uv run python -m gateway    # start the gateway in the foreground
```

## Running the `fitt` CLI

Inside the repo:

```powershell
cd gateway
uv run fitt status          # aliases + current reachability
uv run fitt cost            # MTD spend from gateway.log
uv run fitt config check    # validate config + secrets without starting
```

Or install `fitt` as a global tool so you can run it from anywhere:

```powershell
cd gateway
uv tool install --editable .
fitt status   # works from any shell, any directory
```

Re-run `uv tool install --editable .` after `git pull` to refresh.

## Configuration

Config lives in **`~/.fitt/config.yaml`** (non-secret) and
**`~/.fitt/secrets.yaml`** (secret). The repo ships `.example`
templates under `configs/`; real values never land in git.

### `config.yaml` structure

```yaml
server:
  host: 0.0.0.0
  port: 8080
  log_level: info
  log_bodies: false          # set true to log prompt/response bodies

aliases:
  fitt-default: <model-id>   # local, everyday coding
  fitt-smart:   <model-id>   # cloud, hard turns
  fitt-fast:    <model-id>   # local, cheap helpers

models:
  - id: <string>
    backend: openrouter | anthropic | ollama
    model:   <upstream model name, LiteLLM convention>
    endpoint: <URL>          # required for ollama
    cost_per_mtok_in:  <USD>
    cost_per_mtok_out: <USD>
    fallback: <another model id, optional>

logging:
  dir: ~/.fitt/logs
  retention_days: 30
```

**Client discipline:** the `model` field of a chat-completion request
MUST be one of the `aliases` keys. Concrete names like
`qwen2.5-coder:14b` are rejected with 400. This is what keeps
"models are configuration, not architecture" enforceable.

### `secrets.yaml` structure

```yaml
allowed_tokens:
  - name: personal
    token: <32+ random chars>   # clients send: Authorization: Bearer <this>

openrouter_api_key: sk-or-v1-...

# Optional; uncomment if you enable the `claude-...-direct` model.
# anthropic_api_key: sk-ant-...

# Reserved for Phase 3.
# telegram:
#   bot_token: 123456:ABC-...
#   allowlist_user_ids:
#     - 123456789
```

Refuses to load if group/world-readable on POSIX. On Windows, see
the `icacls` command in the quickstart.

## HTTP API

### `POST /v1/chat/completions` (auth required)

OpenAI-compatible. Forward any request body that Claude or GPT would
accept; the gateway strips `model`, resolves it to an alias, and
forwards the rest to the configured backend via LiteLLM.

**Request:**

```json
{
  "model": "fitt-smart",
  "messages": [{"role": "user", "content": "Hello"}],
  "stream": false,
  "temperature": 0.7
}
```

**Response headers:**

| Header              | Meaning                                              |
|---------------------|------------------------------------------------------|
| `X-FITT-Alias`      | The alias the client asked for.                      |
| `X-FITT-Backend`    | The actual backend that served the request (e.g. `openrouter:anthropic/claude-sonnet-4.5` or `ollama:http://laptop:11434`). |
| `X-FITT-Fallback`   | Present and `1` if the primary was down and fallback was used. |

**Streaming:** set `stream: true`. The response is `text/event-stream`
with standard OpenAI chunk shapes. If the upstream stream fails
mid-response, the gateway emits a `[ERROR]` SSE event rather than
silently truncating.

### `GET /v1/models` (no auth)

OpenAI-compatible model listing with FITT extensions:

```json
{
  "object": "list",
  "data": [
    {
      "id": "fitt-smart",
      "object": "model",
      "created": 1777497338,
      "owned_by": "fitt",
      "fitt_backend": "openrouter",
      "fitt_resolved_model": "anthropic/claude-sonnet-4.5",
      "fitt_fallback": null
    }
  ]
}
```

### `GET /health` (no auth)

`{"status": "ok"}` while the process is alive.

### `GET /ready` (no auth)

Probes every alias's primary + fallback. Returns 200 when every alias
has at least one reachable backend, 503 otherwise with the list of
failing aliases.

## Failure handling

| Upstream behavior                      | Gateway response                                  |
|----------------------------------------|---------------------------------------------------|
| 200 stream                             | Passthrough as SSE                                |
| 200 non-stream                         | Passthrough as JSON                               |
| Connection refused / timeout (primary) | Try fallback once; log failover                   |
| Connection refused / timeout (both)    | 503 + body listing attempted backends             |
| HTTP 429 (rate limited)                | 503 + `Retry-After` header                        |
| HTTP 529 (vendor overloaded)           | 503 + `Retry-After: 30`                           |
| HTTP 4xx (other)                       | Pass through with same status                     |
| HTTP 5xx (other)                       | 502 + body preserving upstream message            |
| Mid-stream connection drop             | SSE stream ends with `[ERROR]` event              |
| Unknown alias                          | 400 + body listing available aliases              |
| Concrete model id instead of alias     | 400 + body explaining alias requirement           |
| Invalid / missing Bearer token         | 401 (no backend call)                             |

## Environment variables

| Variable              | Purpose                                               |
|-----------------------|-------------------------------------------------------|
| `FITT_HOME`           | Override `~/.fitt`. Used by tests and the service install. |
| `FITT_CONFIG_PATH`    | Override the config file location.                    |
| `FITT_SECRETS_PATH`   | Override the secrets file location.                   |

The service installer sets `FITT_HOME` to `%USERPROFILE%\.fitt`
explicitly so the service reads the right files regardless of how
Windows spawns the process.

## Troubleshooting

### `uv sync` fails with a network error

uv downloads Python + dependency wheels from the internet on first
run. Corporate proxies, flaky Wi-Fi, or a stale DNS cache can break
this. Retry, or set `UV_HTTP_TIMEOUT=120`.

### `install-service.ps1` says "Expected Python at gateway\.venv\Scripts\python.exe"

The venv hasn't been created. Either:

```powershell
cd gateway
uv sync
```

or re-run the install script with `-SetupVenv` so it runs `uv sync`
for you.

### The gateway says "secrets.yaml not found"

You haven't copied the templates into `~/.fitt/`. See the quickstart.

### `Get-Service FITTGateway` shows the service running but curl /health hangs

Firewall rule likely wrong. Run:

```powershell
Get-NetFirewallRule -DisplayName 'FITT Gateway (Private only)' | Format-List
netstat -an | findstr :8080
```

- The firewall rule should exist and target Private profile.
- `netstat` should show `0.0.0.0:8080` in LISTENING state.

If Windows thinks Tailscale's network is Public, change it in
Settings -> Network -> Tailscale -> Set as Private.

### 401 Unauthorized from my IDE

Four likely causes, check in order:

1. IDE config is missing the `Authorization: Bearer <token>` header
   entirely. In Continue, the "API key" field sets this.
2. Token in the IDE doesn't match any `allowed_tokens.token` in
   `secrets.yaml`.
3. You edited `secrets.yaml` but didn't restart the gateway.
   Windows service: `Restart-Service FITTGateway`.
4. Trailing whitespace in the token. Regenerate with
   `uv run python -c "import secrets; print(secrets.token_urlsafe(32))"`.

### /ready returns 503 with "fitt-default" failing

The laptop's Ollama isn't reachable from the desktop. Check:

1. Laptop is awake and Tailscale is connected:
   `tailscale ping <laptop-name>`.
2. Ollama is running on the laptop:
   `curl http://<laptop-tailscale-ip>:11434/api/tags` from the
   desktop should return JSON.
3. If step 2 fails: the laptop hasn't picked up
   `OLLAMA_HOST=0.0.0.0`. Quit Ollama from the tray and restart it
   after setting the env var.
4. Endpoint in `config.yaml` matches the laptop's actual Tailscale IP.

### `PermissionError: [Errno 13]` on secrets.yaml

The ACL on `secrets.yaml` ended up wrong. Reset:

```powershell
icacls "$env:USERPROFILE\.fitt\secrets.yaml" /reset
icacls "$env:USERPROFILE\.fitt\secrets.yaml" /inheritance:r /grant:r "$($env:USERNAME):F"
```

### Streaming responses appear garbled or delayed in the IDE

Your client needs to enable streaming for the model. In Continue,
`streaming: true` in the model config. Cursor does this automatically.

### `fitt cost` shows $0 even though I used the cloud alias

Token counts come from the upstream provider's usage data. Some
providers return it only at end-of-stream, which Phase 1 doesn't
fully wire up for streaming responses - the non-streaming path is
accurate, but streaming responses may log `cost_usd: 0`. Known
Phase 1 limitation tracked in the roadmap.

### The gateway starts but a few seconds later the service stops

NSSM crash-loop protection. Check `~/.fitt/logs/service.stderr.log`
and `~/.fitt/logs/gateway.log` for the underlying error. Common:
missing `openrouter_api_key`, invalid YAML, port 8080 already bound.

### How do I update the gateway without losing my config?

```powershell
cd home-ai-cluster
git pull
cd gateway
uv sync
Restart-Service FITTGateway
```

`~/.fitt/` is outside the repo and unaffected.

## Architecture

See [`../.kiro/specs/phase1-gateway/design.md`](../.kiro/specs/phase1-gateway/design.md)
for the full design document: architecture diagram, module breakdown,
design decisions with rationale, correctness properties, and the
testing strategy.
