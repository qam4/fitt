# FITT Quickstart

> One page, start to finish. From nothing to a working gateway reachable
> from your laptop's IDE in about 45 minutes.

This doc walks straight through the setup in order. For deeper
reference, see:

- [`prerequisites.md`](./prerequisites.md) — per-machine software checklist
- [`accounts-setup.md`](./accounts-setup.md) — external accounts
- [`../gateway/README.md`](../gateway/README.md) — full gateway reference

---

## Two machines, one assistant

| Role    | Machine | Runs                                              |
|---------|---------|---------------------------------------------------|
| Hub     | Desktop | FITT gateway (always on), smaller Ollama fallback |
| Compute | Laptop  | Primary Ollama (bigger, VRAM-rich model)          |

Your phone and IDE are clients.

---

## Step 1 — Tailscale on every device (5 min)

Needed on: **Hub, Compute, phone.**

```powershell
# Skip if already installed. Otherwise:
winget install tailscale.tailscale     # or download from tailscale.com
```

Sign in on all three with the same account. Verify:

```powershell
tailscale status
```

All three devices should show IPs in `100.x.x.x`. **Note the Hub's IP
and the Compute's IP — you'll use them in step 5.**

---

## Step 2 — Ollama on Compute (10 min)

On the **laptop**:

1. Install Ollama ([ollama.com/download](https://ollama.com/download)).
2. Set `OLLAMA_HOST=0.0.0.0` — **critical**, or the Hub can't reach it:
   - Windows Settings → "Edit the system environment variables" →
     Environment Variables → New **User variable**.
   - Name: `OLLAMA_HOST`, Value: `0.0.0.0`.
   - Right-click the Ollama tray icon → Quit, then restart Ollama so
     it picks up the env var.
3. Pull the primary model:

   ```powershell
   ollama pull qwen2.5-coder:14b
   ```

4. Verify from the **Hub**:

   ```powershell
   curl http://<compute-tailscale-ip>:11434/api/tags
   ```

   JSON response = good. Connection refused = step 2's env var didn't
   take.

---

## Step 3 — Ollama on Hub (5 min)

On the **desktop** (Hub):

1. Install Ollama, same as step 2.
2. Set `OLLAMA_HOST=0.0.0.0` too (lets you query it from elsewhere if
   you ever want).
3. Pull the fallback model:

   ```powershell
   ollama pull qwen2.5-coder:7b
   ```

---

## Step 4 — External account: OpenRouter (3 min)

1. Go to [openrouter.ai](https://openrouter.ai) and sign in with
   GitHub or Google.
2. [openrouter.ai/keys](https://openrouter.ai/keys) → **Create Key**.
   Name it `fitt-gateway`.
3. Copy the key (starts with `sk-or-v1-...`). You'll paste it in
   step 6.

Optional: add $10 at [openrouter.ai/credits](https://openrouter.ai/credits)
for higher free-model limits (1000/day vs 50/day). Not required.

---

## Step 5 — Gateway install on Hub (10 min)

On the **desktop**.

### 5.1 Python 3.11+

```powershell
python --version        # need 3.11 or newer
```

If missing: [python.org/downloads](https://www.python.org/downloads/),
check "Add Python to PATH".

### 5.2 NSSM (service wrapper)

```powershell
choco install nssm
# or scoop install nssm
# or grab the binary from https://nssm.cc/download and put it on PATH
```

### 5.3 Clone and install the gateway

```powershell
cd $env:USERPROFILE
gh repo clone qam4/home-ai-cluster
cd home-ai-cluster\gateway

python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -e .
```

Don't `pip install -e ".[dev]"` on a clean install — that pulls test
deps you don't need for running the service.

---

## Step 6 — Config and secrets (10 min)

Still on the Hub.

### 6.1 Seed `~/.fitt/`

```powershell
cd $env:USERPROFILE\home-ai-cluster
mkdir $env:USERPROFILE\.fitt
Copy-Item configs\config.example.yaml  $env:USERPROFILE\.fitt\config.yaml
Copy-Item configs\secrets.example.yaml $env:USERPROFILE\.fitt\secrets.yaml
```

### 6.2 Edit `~/.fitt/config.yaml`

```powershell
notepad $env:USERPROFILE\.fitt\config.yaml
```

Replace the placeholder Tailscale IP in the `qwen-coder-big` model:

```yaml
  - id: qwen-coder-big
    backend: ollama
    endpoint: http://<compute-tailscale-ip>:11434   # laptop's 100.x.x.x
    model: qwen2.5-coder:14b
    fallback: qwen-coder-small
```

Everything else can stay default. Save.

### 6.3 Generate a Bearer token

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copy the long random string that prints.

### 6.4 Edit `~/.fitt/secrets.yaml`

```powershell
notepad $env:USERPROFILE\.fitt\secrets.yaml
```

Paste in:

- The Bearer token from 6.3 into `allowed_tokens[0].token`.
- The OpenRouter key from step 4 into `openrouter_api_key`.

Leave the Anthropic and Telegram blocks commented out for now. Save.

### 6.5 Lock down permissions

```powershell
icacls "$env:USERPROFILE\.fitt\secrets.yaml" /inheritance:r /grant:r "$($env:USERNAME):(R,W)"
```

### 6.6 Sanity check

```powershell
cd $env:USERPROFILE\home-ai-cluster\gateway
.venv\Scripts\python -m gateway.cli config check
```

Should print `Configuration OK.` with your aliases and models listed.
If it errors, fix what it points at and re-run.

---

## Step 7 — Install as a Windows service (5 min)

Open an **elevated** PowerShell (right-click → Run as administrator).

```powershell
cd $env:USERPROFILE\home-ai-cluster
.\scripts\install-service.ps1
```

This:

- registers `FITTGateway` via NSSM with auto-start and 30-second
  restart on failure,
- adds a Windows Defender Firewall rule (TCP 8080, Private profile
  only),
- runs a `/health` probe and reports the result.

Verify:

```powershell
Get-Service FITTGateway
curl http://localhost:8080/health             # should print {"status":"ok"}
curl http://localhost:8080/v1/models          # should list your aliases
curl http://<hub-tailscale-ip>:8080/health    # same, over Tailscale
```

---

## Step 8 — Wire up your IDE (5 min)

On the **laptop**, in VS Code.

### 8.1 Install Continue

Install the **Continue** extension from the marketplace if not
already.

### 8.2 Configure a custom OpenAI-compatible provider

Open Continue's config (Ctrl+Shift+P → "Continue: Open Config") and
add under `models`:

```json
{
  "title": "FITT smart (Claude via OpenRouter)",
  "provider": "openai",
  "model": "fitt-smart",
  "apiBase": "http://<hub-tailscale-ip>:8080/v1",
  "apiKey": "<your-bearer-token-from-6.3>"
},
{
  "title": "FITT default (local Qwen)",
  "provider": "openai",
  "model": "fitt-default",
  "apiBase": "http://<hub-tailscale-ip>:8080/v1",
  "apiKey": "<your-bearer-token-from-6.3>"
}
```

### 8.3 Test a chat

In Continue's chat pane:

- Select **FITT smart** from the model dropdown.
- Ask "what's 2+2?" — a real cloud model should answer.
- Switch to **FITT default** — the laptop's Ollama should answer.

---

## Step 9 — Verify end to end (5 min)

On the Hub:

```powershell
# How the gateway looks to the world right now
.venv\Scripts\python -m gateway.cli status

# Did those test chats land?
.venv\Scripts\python -m gateway.cli cost
```

From your **phone** (on Tailscale, not mobile data):

```
http://<hub-tailscale-ip>:8080/v1/models
```

Open in browser. Should return JSON. If you can see this from the
phone, the full stack is working.

From your **phone on mobile data** (off the tailnet), run:

```
https://<hub-public-ip>:8080/
```

Should **fail** to connect. If it succeeds, the firewall rule is
wrong — revisit step 7.

---

## Step 10 — Resilience checks (5 min)

On the Hub, elevated PowerShell:

```powershell
# Restart test
Restart-Computer
# After reboot, from anywhere on Tailscale:
curl http://<hub-tailscale-ip>:8080/health      # should respond within 60s

# Crash test — find the PID, kill it, watch NSSM restart
Get-Process python | Where-Object { $_.Path -like "*home-ai-cluster*" }
Stop-Process -Id <pid>
Start-Sleep -Seconds 35
curl http://localhost:8080/health               # should respond again
```

---

## You're done

What you have now:

- A Windows service on the Hub that routes chat requests between
  local Ollama and OpenRouter.
- IDE chat on the laptop that can reach it.
- One Bearer token, one config, swappable models.
- Logs in `~/.fitt/logs/gateway.log` (tail with your favorite tool).
- Cost visibility with `fitt cost`.

Next: live with it for two weeks before starting Phase 2 (memory) —
see [`../FITT_ROADMAP.md`](../FITT_ROADMAP.md) guiding principle 9.

## Troubleshooting

If something's wrong, `gateway/README.md` has a dedicated
troubleshooting section: auth 401, `/ready` 503, streaming cost=0,
service crash loops, firewall issues, update workflow.

## Common slip-ups (you've been warned)

- **`OLLAMA_HOST=0.0.0.0`** not taking effect on the laptop: you
  changed the env var but didn't restart Ollama.
- **Firewall rule on Public profile**: Windows marked your Tailscale
  interface as Public. Settings → Network → Tailscale → Private.
- **Trailing whitespace in the Bearer token**: copy-paste ate a space.
  Regenerate with the one-liner in 6.3.
- **Wrong Tailscale IP in `config.yaml`**: your laptop got a new IP
  after sleep/wake. Use MagicDNS (hostnames) if this happens often.
- **`pip install -e .` fails**: you're missing Python 3.11+, or pip
  itself is outdated (`python -m pip install --upgrade pip`).
