# FITT Gateway

OpenAI-compatible HTTP gateway that routes chat-completion requests
between local Ollama models and cloud providers (OpenRouter, optionally
Anthropic) based on the alias the client asks for.

## Install (development)

```bash
cd gateway
python -m venv .venv
.venv\Scripts\activate       # Windows
# source .venv/bin/activate  # POSIX

pip install -e ".[dev]"
pytest
```

## Configuration

1. Copy `../configs/config.example.yaml` to `~/.fitt/config.yaml`.
2. Copy `../configs/secrets.example.yaml` to `~/.fitt/secrets.yaml`.
3. Restrict permissions on the secrets file:

   ```powershell
   # Windows
   icacls "$env:USERPROFILE\.fitt\secrets.yaml" /inheritance:r /grant:r "$($env:USERNAME):(R,W)"
   ```

4. Fill in your OpenRouter API key, a random Bearer token, and the
   Tailscale IP of your laptop.

See [`../docs/accounts-setup.md`](../docs/accounts-setup.md) for
step-by-step guidance on account creation and key collection.

## Run

```bash
fitt-gateway
```

Listens on `http://0.0.0.0:8080` by default. Configure with
`~/.fitt/config.yaml`.

## Usage

```bash
curl -H "Authorization: Bearer <your-token>" \
     -H "Content-Type: application/json" \
     -X POST http://localhost:8080/v1/chat/completions \
     -d '{
       "model": "fitt-smart",
       "messages": [{"role": "user", "content": "hello"}]
     }'
```

`fitt-smart`, `fitt-default`, `fitt-fast` are aliases configured in
`config.yaml`. Never pass concrete model IDs like `qwen2.5-coder:14b`
— the gateway rejects them.

## CLI

```bash
fitt status         # list aliases and their reachability
fitt cost           # month-to-date spend aggregated from logs
fitt config check   # validate config and secrets without starting
```

## Endpoints

| Path                        | Auth | Purpose                                    |
|-----------------------------|------|--------------------------------------------|
| `GET /health`               | no   | liveness                                   |
| `GET /ready`                | no   | readiness (backends reachable)             |
| `GET /v1/models`            | no   | list aliases                               |
| `POST /v1/chat/completions` | yes  | main chat endpoint (OpenAI-compatible)     |

See [`../.kiro/specs/phase1-gateway/design.md`](../.kiro/specs/phase1-gateway/design.md)
for architecture and the failure-handling table.
