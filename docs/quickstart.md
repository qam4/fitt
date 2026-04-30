# FITT Quickstart

> One page, start to finish. From nothing to a working gateway
> reachable from your laptop's IDE in about 30 minutes.

This is the only install doc. If you want to set up FITT, read this
top to bottom and do each step. For architecture and design, see
[`../FITT_ROADMAP.md`](../FITT_ROADMAP.md). For gateway internals
after install, see [`../gateway/README.md`](../gateway/README.md).

---

## Two machines, one assistant

| Role    | Machine | Runs                                              |
|---------|---------|---------------------------------------------------|
| Hub     | Desktop | FITT gateway (always on), smaller Ollama fallback |
| Compute | Laptop  | Primary Ollama (bigger, VRAM-rich model)          |

Your phone and IDE are clients.

---

## Step 1 - Tailscale on every device (5 min)

Needed on: **Hub, Compute, phone.**

```powershell
# Skip if already installed. Otherwise:
winget install --id=tailscale.tailscale -e     # or download from tailscale.com
```

Sign in on all three with the same account. Verify:

```powershell
tailscale status
```

All three devices should show IPs in `100.x.x.x`. **Note the Hub's
IP and the Compute's IP - you'll use them in step 5.**

Tailscale's default ACL already allows every device on your tailnet
to reach every other device. Don't tighten ACLs unless you know
exactly which ports to allow.

---

## Step 2 - Ollama on Compute (10 min)

On the **laptop**:

1. Install Ollama ([ollama.com/download](https://ollama.com/download)).
2. Set `OLLAMA_HOST=0.0.0.0` - **critical**, or the Hub can't reach
   it:
   - Windows Settings -> "Edit the system environment variables" ->
     Environment Variables -> New **User variable**.
   - Name: `OLLAMA_HOST`, Value: `0.0.0.0`.
   - Right-click the Ollama tray icon -> Quit, then restart Ollama
     so it picks up the env var.
3. Pull the primary model:

   ```powershell
   ollama pull qwen2.5-coder:14b
   ```

4. Verify from the **Hub**:

   ```powershell
   curl http://<compute-tailscale-ip>:11434/api/tags
   ```

   JSON response = good. Connection refused = step 2.2's env var
   didn't take.

---

## Step 3 - Ollama on Hub (5 min)

On the **desktop** (Hub):

1. Install Ollama, same as step 2.
2. Set `OLLAMA_HOST=0.0.0.0` too (lets you query it from elsewhere
   if you ever want).
3. Pull the fallback model:

   ```powershell
   ollama pull qwen2.5-coder:7b
   ```

Smaller model, fits the Hub's 8 GB VRAM comfortably. Used as the
fallback when Compute is asleep.

---

## Step 4 - Cloud LLM account: OpenRouter (3 min)

FITT uses OpenRouter as its cloud backend - one API key gives access
to many models (Claude, GPT, Gemini, open-source) with a free tier.

1. Go to [openrouter.ai](https://openrouter.ai) and sign in with
   GitHub or Google.
2. [openrouter.ai/keys](https://openrouter.ai/keys) -> **Create
   Key**. Name it `fitt-gateway`.
3. Copy the key (starts with `sk-or-v1-...`). You'll paste it in
   step 6.

Optional: add $10 at
[openrouter.ai/credits](https://openrouter.ai/credits) for higher
free-model limits (1000/day vs 50/day). Not required for v0.

### Optional: Anthropic direct (skip for v0)

The gateway supports direct Anthropic access as an alternate cloud
backend, but v0 uses OpenRouter. Revisit later only if OpenRouter's
Claude routes stop meeting your needs. Then:

1. Sign up at [console.anthropic.com](https://console.anthropic.com).
2. Set a monthly spend limit (Usage limits -> Monthly limit).
3. API Keys -> Create Key.
4. Uncomment `anthropic_api_key` in `~/.fitt/secrets.yaml` and the
   `claude-sonnet-direct` block in `~/.fitt/config.yaml`.

---

## Step 5 - Install uv and NSSM on Hub (5 min)

On the **desktop**.

### 5.1 Install uv

uv manages Python, the virtual environment, and dependencies in one
tool. You don't install Python yourself - uv downloads a compatible
one when it needs to.

```powershell
winget install --id=astral-sh.uv -e
```

Or the official installer (use if winget is unavailable):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Restart your shell so `uv` is on PATH, then verify:

```powershell
uv --version
```

uv does not put a `python.exe` on PATH, so it doesn't conflict with
any existing Python install, pyenv, conda, etc.

### 5.2 Install NSSM

NSSM wraps the gateway as a proper Windows service with auto-restart.

```powershell
winget install --id=NSSM.NSSM -e
# or: choco install nssm
# or: scoop install nssm
# or grab the binary from https://nssm.cc/download and put it on PATH
```

### 5.3 Clone the repo

```powershell
cd $env:USERPROFILE
gh repo clone qam4/home-ai-cluster
```

---

## Step 6 - Config and secrets on Hub (10 min)

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
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copy the long random string that prints.

### 6.4 Edit `~/.fitt/secrets.yaml`

```powershell
notepad $env:USERPROFILE\.fitt\secrets.yaml
```

Paste in:

- The Bearer token from 6.3 into `allowed_tokens[0].token`.
- The OpenRouter key from step 4 into `openrouter_api_key`.

Leave the Anthropic and Telegram blocks commented out. Save.

### 6.5 Lock down permissions

```powershell
icacls "$env:USERPROFILE\.fitt\secrets.yaml" /inheritance:r /grant:r "$($env:USERNAME):F"
```

This removes inherited ACEs and grants full control to just you. If
you later see `PermissionError: [Errno 13]` loading the gateway, the
ACL ended up wrong. Reset with:

```powershell
icacls "$env:USERPROFILE\.fitt\secrets.yaml" /reset
icacls "$env:USERPROFILE\.fitt\secrets.yaml" /inheritance:r /grant:r "$($env:USERNAME):F"
```

### 6.6 Sanity check

```powershell
cd $env:USERPROFILE\home-ai-cluster\gateway
uv sync
uv run fitt config check
```

`uv sync` creates `gateway\.venv`, downloads a Python if needed, and
installs the gateway's dependencies. Subsequent runs are fast.

`uv run fitt config check` should print `Configuration OK.` with
your aliases and models listed. If it errors, fix what it points at
and re-run.

---

## Step 7 - Install as a Windows service (5 min)

Open an **elevated** PowerShell (right-click -> Run as administrator).

```powershell
cd $env:USERPROFILE\home-ai-cluster
.\scripts\install-service.ps1 -SetupVenv
```

`-SetupVenv` runs `uv sync` inside this elevated shell, guaranteeing
the service points at the right Python regardless of your user PATH.
Without `-SetupVenv`, the script expects step 6.6's `uv sync` to
have already run and simply uses the existing `gateway\.venv`.

The script:

- Verifies the venv's Python can actually `import gateway`. Fails
  fast with a clear error if not.
- Registers `FITTGateway` via NSSM with auto-start and 30-second
  restart on failure.
- Adds a Windows Defender Firewall rule (TCP 8080, Private profile
  only).
- Polls `/health` for up to 45 seconds and reports the result.

Verify:

```powershell
Get-Service FITTGateway
curl http://localhost:8080/health             # should print {"status":"ok"}
curl http://localhost:8080/v1/models          # should list your aliases
curl http://<hub-tailscale-ip>:8080/health    # same, over Tailscale
```

**First boot takes 15-30 seconds** while Python imports LiteLLM,
pydantic, etc. If `/health` doesn't respond immediately, wait a bit
and retry; NSSM will restart the process if it actually crashed.

---

## Step 8 - Wire up your IDE (5 min)

On the **laptop**, in VS Code.

### 8.1 Install Continue

Install the **Continue** extension from the marketplace.

### 8.2 Add FITT aliases to Continue's config.yaml

Continue stores its configuration at
`%USERPROFILE%\.continue\config.yaml` on Windows
(`~/.continue/config.yaml` on POSIX). Open it in your editor and add
two model entries under `models:`:

```yaml
models:
  - name: FITT smart (Claude via OpenRouter)
    provider: openai
    apiBase: http://<hub-tailscale-ip>:8080/v1
    apiKey: <your-bearer-token-from-6.3>
    model: fitt-smart
    roles:
      - chat
      - edit
      - apply

  - name: FITT default (local Qwen)
    provider: openai
    apiBase: http://<hub-tailscale-ip>:8080/v1
    apiKey: <your-bearer-token-from-6.3>
    model: fitt-default
    roles:
      - chat
      - edit
      - apply
```

`provider: openai` plus `apiBase` points Continue at any
OpenAI-compatible server, which the gateway is. Despite the name,
this does not mean "send to OpenAI.com" - Continue uses `openai` as
the generic identifier for any backend that speaks the OpenAI HTTP
API shape.

You can also reach `config.yaml` through the UI: click the Continue
icon in the VS Code sidebar, click the gear (settings) icon at the
top-right of the Continue panel, then the **Configs** tab.

### 8.3 Test a chat

Reload Continue (close and reopen the panel, or reload the VS Code
window). In Continue's chat pane:

- Select **FITT smart** from the model dropdown.
- Ask "what's 2+2?" - a real cloud model should answer.
- Switch to **FITT default** - the laptop's Ollama should answer.

---

## Step 9 - Verify end to end (5 min)

On the Hub:

```powershell
cd $env:USERPROFILE\home-ai-cluster\gateway
uv run fitt status          # aliases + current reachability
uv run fitt cost            # month-to-date spend
```

From your **phone** (on Tailscale, not mobile data), open in browser:

```
http://<hub-tailscale-ip>:8080/v1/models
```

Should return JSON. If you can see this from the phone, the full
stack is working.

From your **phone on mobile data** (off the tailnet), try:

```
http://<hub-public-ip>:8080/
```

Should **fail** to connect. If it succeeds, the firewall rule is
wrong - revisit step 7.

---

## Step 10 - Resilience checks (5 min)

On the Hub, elevated PowerShell:

```powershell
# Restart test
Restart-Computer
# After reboot, from anywhere on Tailscale:
curl http://<hub-tailscale-ip>:8080/health      # should respond within 60s

# Crash test - find the PID, kill it, watch NSSM restart
Get-Process python | Where-Object { $_.Path -like "*home-ai-cluster*" }
Stop-Process -Id <pid>
Start-Sleep -Seconds 35
curl http://localhost:8080/health               # should respond again
```

---

## Telegram bot (optional, used in Phase 3)

Phase 1 doesn't read Telegram credentials, but you can populate them
now so you don't have to touch `secrets.yaml` again when Phase 3
ships.

1. On Telegram, message `@BotFather`. `/newbot`, follow prompts.
   Save the bot token (looks like `123456:ABC-...`).
2. On Telegram, message `@userinfobot` to get your numeric user ID.
3. Edit `~/.fitt/secrets.yaml` and uncomment the Telegram block:

   ```yaml
   telegram:
     bot_token: 123456:ABC-xxxxxxxxxxxxx
     allowlist_user_ids:
       - 123456789
   ```

---

## Step 11 - Telegram bot install (5 min)

Install the bot service as a sibling of the gateway. From an
**elevated** PowerShell at the repo root:

```powershell
.\scripts\install-telegram-bot.ps1 -SetupVenv
```

This:

- runs `uv sync` in `telegram-bot\` to create its venv,
- verifies the bot can `import fitt_telegram_bot`,
- registers `FITTTelegramBot` as a Windows service with
  auto-start and 30-second restart on failure.

No firewall rule is needed - the bot makes outbound connections
only (Telegram Bot API + localhost gateway).

Verify:

```powershell
Get-Service FITTTelegramBot
Get-Content "$env:USERPROFILE\.fitt\logs\telegram-bot.stdout.log" -Tail 20
```

Then open Telegram on your phone, find your bot, send `/start`.
You should get a welcome message showing your current alias and
session. If nothing happens, check:

1. Is your Telegram user id in `allowlist_user_ids`? Non-allowlisted
   users are silently dropped by design.
2. Is the bot token correct? `@BotFather` -> `/mybots` -> your bot
   -> **API Token**.
3. Is the gateway healthy (`curl http://localhost:8080/health`)?

## Step 12 - Open WebUI install (5 min)

Needs Docker Desktop running on the Hub. If not installed:

```powershell
winget install --id=Docker.DockerDesktop -e
# Start Docker Desktop and sign in if prompted.
```

Then, elevated PowerShell at the repo root:

```powershell
.\scripts\install-open-webui.ps1
```

This reads your Bearer token from `secrets.yaml`, writes a
gitignored `.env` next to `docker-compose.yml`, brings up the
container, and adds the Tailscale-scoped firewall rule for TCP
3000.

Verify from the Hub:

```
http://localhost:3000/
```

Or from your phone on Tailscale:

```
http://<hub-tailscale-ip>:3000/
```

First visit shows the admin signup. Create your admin account. The
container then disables further signups (`ENABLE_SIGNUP=false`), so
random visitors on your tailnet cannot register.

In Open WebUI, pick any of the FITT aliases (`fitt-default`,
`fitt-smart`, ...) from the model dropdown. Responses flow through
the gateway, so they show up in `fitt cost` alongside your Telegram
traffic.

---

## You're done

What you have now:

- A Windows service on the Hub that routes chat requests between
  local Ollama and OpenRouter.
- IDE chat on the laptop that can reach it.
- One Bearer token, one config, swappable models.
- Logs in `~/.fitt/logs/gateway.log`.
- Cost visibility with `uv run fitt cost`.

Next: live with it for a week or two before starting the next phase
- see [`../FITT_ROADMAP.md`](../FITT_ROADMAP.md) guiding principle 9.

---

## Troubleshooting

`gateway/README.md` has a dedicated troubleshooting section: auth
401, `/ready` 503, streaming cost=0, service crash loops, firewall
issues, update workflow.

## Common slip-ups

- **`OLLAMA_HOST=0.0.0.0`** not taking effect on the laptop: you
  changed the env var but didn't restart Ollama.
- **Firewall rule on Public profile**: Windows marked your Tailscale
  interface as Public. Settings -> Network -> Tailscale -> Private.
- **Trailing whitespace in the Bearer token**: copy-paste ate a
  space. Regenerate with the one-liner in 6.3.
- **Wrong Tailscale IP in `config.yaml`**: your laptop got a new IP
  after sleep/wake. Use MagicDNS (hostnames) if this happens often.
- **`uv sync` fails with network errors**: uv downloads Python +
  deps from the internet; corporate proxies or flaky Wi-Fi can trip
  it up. Retry, or set `UV_HTTP_TIMEOUT=120`.
- **`install-service.ps1` reports "Expected Python at ... \gateway\.venv\..."**:
  run `uv sync` in `gateway\` first, or re-run the script with
  `-SetupVenv`.
