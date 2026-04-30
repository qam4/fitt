# FITT Quickstart

> One page, start to finish. From nothing to a working gateway
> reachable from your laptop's IDE in about 30 minutes.

This is the only install doc. If you want to set up FITT, read this
top to bottom and do each step. For architecture and design, see
[`../FITT_ROADMAP.md`](../FITT_ROADMAP.md). For gateway internals
after install, see [`../gateway/README.md`](../gateway/README.md).

---

## Roles

| Role      | Count       | Runs                                                    |
|-----------|-------------|---------------------------------------------------------|
| Hub       | exactly one | FITT gateway (always on), Telegram bot, Open WebUI, optional small Ollama fallback |
| Satellite | one or more | Ollama hosting one or more local models                 |
| Client    | any number  | VS Code + Continue, phone browser, Telegram app, Open WebUI browser |

A machine can play more than one role. For a solo setup the hub
often also runs a small Ollama (satellite-on-hub), and your laptop
is both a satellite and a client. The roles are logical, not
physical.

The doc below walks through them in that order: **Hub first**,
then **Satellites**, then **Clients**. You need the Hub booted
before a Satellite is useful, and the Hub plus at least the cloud
backend working before a Client is useful.

---

# Part A — Hub

One machine, always on. Desktop or mini-PC works. The Hub hosts the
gateway, the Telegram bot, and Open WebUI.

## Step 1 - Tailscale on the Hub (3 min)

```powershell
winget install --id=tailscale.tailscale -e     # or download from tailscale.com
```

Sign in. Verify:

```powershell
tailscale status
```

Note the Hub's `100.x.x.x` IP. You'll hand it to clients in Part C.

Tailscale's default ACL already allows every device on your tailnet
to reach every other device. Don't tighten ACLs unless you know
exactly which ports to allow.

## Step 2 - Cloud LLM account: OpenRouter (3 min)

FITT uses OpenRouter as its cloud backend - one API key gives
access to many models (Claude, GPT, Gemini, open-source) with a
free tier.

1. Go to [openrouter.ai](https://openrouter.ai) and sign in with
   GitHub or Google.
2. [openrouter.ai/keys](https://openrouter.ai/keys) -> **Create
   Key**. Name it `fitt-gateway`.
3. Copy the key (starts with `sk-or-v1-...`). You'll paste it in
   step 5.

Optional: add $10 at
[openrouter.ai/credits](https://openrouter.ai/credits) for higher
free-model limits (1000/day vs 50/day). Not required for v0.

### Optional: other OpenAI-compatible providers

The gateway has a generic `openai` backend you can point at any
provider speaking the OpenAI schema: Nvidia Build (free tier),
Groq, Together, Fireworks, LM Studio, vLLM. See
`configs/config.example.yaml` and the gateway README for the
config shape. Per-model keys go under `api_keys:` in
`secrets.yaml`. Skip for v0; add later as needed.

### Optional: Anthropic direct

The gateway also supports direct Anthropic access. v0 uses
OpenRouter. Revisit later only if OpenRouter's Claude routes stop
meeting your needs, then uncomment the `anthropic_api_key` line in
`secrets.yaml` and the `claude-sonnet-direct` block in
`config.yaml`.

## Step 3 - Install uv and NSSM (5 min)

### 3.1 uv

uv manages Python, the virtual environment, and dependencies in
one tool. You don't install Python yourself - uv downloads a
compatible one when it needs to.

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

uv does not put a `python.exe` on PATH, so it doesn't conflict
with any existing Python install, pyenv, conda, etc.

### 3.2 NSSM

NSSM wraps the gateway as a proper Windows service with
auto-restart.

```powershell
winget install --id=NSSM.NSSM -e
# or: choco install nssm
# or: scoop install nssm
# or grab the binary from https://nssm.cc/download and put it on PATH
```

### 3.3 Clone the repo

```powershell
cd $env:USERPROFILE
gh repo clone qam4/home-ai-cluster
```

## Step 4 - (Optional) Small Ollama fallback on the Hub (5 min)

Lets the Hub answer local-model requests when every satellite is
asleep. Skip if the Hub has no GPU and you don't want CPU
inference.

1. Install Ollama ([ollama.com/download](https://ollama.com/download)).
2. In Windows env vars, set `OLLAMA_HOST=0.0.0.0` (User variable).
   Quit Ollama from the tray and relaunch so it picks up the env
   var.
3. Pull a small model that fits the Hub's VRAM:

   ```powershell
   ollama pull qwen2.5-coder:7b
   ```

The default config uses this as the fallback for the `fitt-default`
alias. If you skip this step, edit `config.yaml` in step 5.2 to
remove the `fallback: qwen-coder-small` line and the
`qwen-coder-small` model block.

## Step 5 - Config and secrets (10 min)

### 5.1 Seed `~/.fitt/`

```powershell
cd $env:USERPROFILE\home-ai-cluster
mkdir $env:USERPROFILE\.fitt
Copy-Item configs\config.example.yaml  $env:USERPROFILE\.fitt\config.yaml
Copy-Item configs\secrets.example.yaml $env:USERPROFILE\.fitt\secrets.yaml
```

### 5.2 Edit `~/.fitt/config.yaml`

```powershell
notepad $env:USERPROFILE\.fitt\config.yaml
```

The example config references a satellite you haven't set up yet.
Two choices:

- **If you'll do Part B right after this:** leave the `100.x.y.z`
  placeholder for now, write it down as "TODO", and come back after
  the satellite is up. The gateway will still start - `/ready` will
  return 503 until the satellite is reachable, but the cloud alias
  will work immediately.
- **If the Hub is Hub-only (no local models):** delete the
  `qwen-coder-big` model block and change `fitt-default:` to point
  at `qwen-coder-small` (or delete `fitt-default` entirely).

Save.

### 5.3 Generate a Bearer token

```powershell
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copy the long random string.

### 5.4 Edit `~/.fitt/secrets.yaml`

```powershell
notepad $env:USERPROFILE\.fitt\secrets.yaml
```

Paste in:

- The Bearer token from 5.3 into `allowed_tokens[0].token`.
- The OpenRouter key from step 2 into `openrouter_api_key`.

Leave the Anthropic, `api_keys`, and Telegram blocks commented out.
Save.

### 5.5 Lock down permissions

```powershell
icacls "$env:USERPROFILE\.fitt\secrets.yaml" /inheritance:r /grant:r "$($env:USERNAME):F"
```

This removes inherited ACEs and grants full control to just you.
If you later see `PermissionError: [Errno 13]` loading the gateway,
the ACL ended up wrong. Reset with:

```powershell
icacls "$env:USERPROFILE\.fitt\secrets.yaml" /reset
icacls "$env:USERPROFILE\.fitt\secrets.yaml" /inheritance:r /grant:r "$($env:USERNAME):F"
```

### 5.6 Sanity check

```powershell
cd $env:USERPROFILE\home-ai-cluster\gateway
uv sync
uv run fitt config check
```

`uv sync` creates `gateway\.venv`, downloads a Python if needed,
and installs the gateway's dependencies. Subsequent runs are fast.

`uv run fitt config check` should print `Configuration OK.` with
your aliases and models listed. It validates the file shape but
does NOT probe endpoints, so a yet-to-be-configured satellite will
not fail here. If it errors, fix what it points at and re-run.

## Step 6 - Install as a Windows service (5 min)

Open an **elevated** PowerShell (right-click -> Run as
administrator).

```powershell
cd $env:USERPROFILE\home-ai-cluster
.\scripts\install-service.ps1 -SetupVenv
```

`-SetupVenv` runs `uv sync` inside this elevated shell,
guaranteeing the service points at the right Python regardless of
your user PATH. Without `-SetupVenv`, the script expects step 5.6's
`uv sync` to have already run and simply uses the existing
`gateway\.venv`.

The script:

- Verifies the venv's Python can actually `import gateway`. Fails
  fast with a clear error if not.
- Registers `FITTGateway` via NSSM with auto-start and 30-second
  restart on failure.
- Adds a Windows Defender Firewall rule (TCP 8080, Private profile
  only).
- Polls `/health` for up to 45 seconds and reports the result.

## Step 7 - Verify the gateway (2 min)

```powershell
Get-Service FITTGateway
curl http://localhost:8080/health             # should print {"status":"ok"}
curl http://localhost:8080/v1/models          # should list your aliases
curl http://<hub-tailscale-ip>:8080/health    # same, over Tailscale
```

**First boot takes 15-30 seconds** while Python imports LiteLLM,
pydantic, etc. If `/health` doesn't respond immediately, wait a
bit and retry; NSSM will restart the process if it actually
crashed.

`/ready` (as opposed to `/health`) will return 503 until every
alias has at least one reachable backend. That's expected if your
satellite isn't up yet - move to Part B.

---

# Part B — Satellites

Any machine (Hub included, see Step 4) that hosts local models. You
can have zero, one, or many. Each satellite contributes one or more
models identified by their Ollama tag.

Do this section **once per satellite**.

## Step 8 - Tailscale on the satellite (3 min)

```powershell
winget install --id=tailscale.tailscale -e
```

Sign in on the same Tailscale account as the Hub. Verify:

```powershell
tailscale status
```

Note this satellite's `100.x.x.x` IP. You'll use it in step 11.

## Step 9 - Ollama on the satellite (5 min)

1. Install Ollama
   ([ollama.com/download](https://ollama.com/download)).
2. Set `OLLAMA_HOST=0.0.0.0` as a **User variable** in Windows env
   vars. Without this, the Hub can't reach Ollama over the tailnet;
   Ollama defaults to listening on loopback only.
3. Quit Ollama from the tray and relaunch so it picks up the env
   var.

Verify from the **Hub**:

```powershell
curl http://<satellite-tailscale-ip>:11434/api/tags
```

JSON response = good. Connection refused = step 9.2's env var
didn't take (most common cause: didn't restart Ollama after
setting it).

## Step 10 - Pull a model (time varies)

On the satellite, pull whatever model this machine is big enough
to serve. Example for a 12-16 GB VRAM laptop:

```powershell
ollama pull qwen2.5-coder:14b
```

Pick the tag based on the VRAM you have. Bigger models = better
answers but slower and VRAM-hungry. The tag you use here is the
`model:` value you'll write into `config.yaml` next.

## Step 11 - Wire the satellite into the Hub

On the **Hub**, edit `~/.fitt/config.yaml`:

```powershell
notepad $env:USERPROFILE\.fitt\config.yaml
```

Add (or update) a model entry pointing at this satellite:

```yaml
models:
  - id: qwen-coder-big             # unique within this file
    backend: ollama
    endpoint: http://<satellite-tailscale-ip>:11434
    model: qwen2.5-coder:14b       # exactly the ollama tag from step 10
    fallback: qwen-coder-small     # optional; another model id in this file
```

Then make sure at least one alias points at it:

```yaml
aliases:
  fitt-default: qwen-coder-big     # everyday coding (this satellite)
```

Save. Restart the gateway so it picks up the new config:

```powershell
Restart-Service FITTGateway
```

Verify from the Hub:

```powershell
uv run fitt status       # should show the satellite as reachable
curl http://localhost:8080/ready      # should return 200 once every alias has a live backend
```

If `/ready` still returns 503, look at `~/.fitt/logs/gateway.log`
for which alias failed and why.

## Adding another satellite later

Want to add a second machine (work laptop, second desktop,
someone else's box)? Just repeat **Part B** on it:

1. Tailscale (step 8).
2. Ollama + `OLLAMA_HOST=0.0.0.0` (step 9).
3. `ollama pull <some-tag>` (step 10).
4. On the Hub, add a new model block to `config.yaml` with a new
   `id`, the new satellite's IP, and the tag you pulled. Bind it
   to an alias (or use it as another model's `fallback`).
5. `Restart-Service FITTGateway`.

The alias pattern means clients don't care which satellite serves
them; they just ask for `fitt-default` or whatever and the gateway
figures out the rest.

---

# Part C — Clients

Anything that calls the gateway. The most common ones:

- VS Code + Continue on your laptop (IDE chat, edit, apply).
- Your phone's browser (quick curl-style checks, Open WebUI).
- Telegram app on your phone (via the FITT Telegram bot, which
  itself runs on the Hub).

## Step 12 - Tailscale on each client device (3 min per device)

Install Tailscale, sign in on the same account, verify
`tailscale status`. Your phone needs the Tailscale app from the App
Store or Play Store.

## Step 13 - VS Code + Continue (5 min)

On your laptop (the machine where you want IDE chat).

### 13.1 Install Continue

Install the **Continue** extension from the VS Code marketplace.

### 13.2 Add FITT aliases to Continue's config.yaml

Continue stores its configuration at
`%USERPROFILE%\.continue\config.yaml` on Windows
(`~/.continue/config.yaml` on POSIX). Open it in your editor and
add two model entries under `models:`:

```yaml
models:
  - name: FITT smart (Claude via OpenRouter)
    provider: openai
    apiBase: http://<hub-tailscale-ip>:8080/v1
    apiKey: <your-bearer-token-from-5.3>
    model: fitt-smart
    roles:
      - chat
      - edit
      - apply

  - name: FITT default (local Qwen)
    provider: openai
    apiBase: http://<hub-tailscale-ip>:8080/v1
    apiKey: <your-bearer-token-from-5.3>
    model: fitt-default
    roles:
      - chat
      - edit
      - apply
```

`provider: openai` plus `apiBase` points Continue at any
OpenAI-compatible server, which the gateway is. Despite the name,
this does not mean "send to OpenAI.com" - Continue uses `openai`
as the generic identifier for any backend that speaks the OpenAI
HTTP API shape.

You can also reach `config.yaml` through the UI: click the
Continue icon in the VS Code sidebar, click the gear (settings)
icon at the top-right of the Continue panel, then the **Configs**
tab.

### 13.3 Test a chat

Reload Continue (close and reopen the panel, or reload the VS
Code window). In Continue's chat pane:

- Select **FITT smart** from the model dropdown. Ask "what's
  2+2?" - a cloud model should answer.
- Switch to **FITT default** - your satellite's Ollama should
  answer.

## Step 14 - Verify from your phone (3 min)

From your **phone on Tailscale** (not mobile data), open in the
browser:

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
wrong - revisit step 6.

## Step 15 - Telegram bot (optional)

The bot runs on the **Hub** and lets you chat with FITT from the
Telegram app on any device.

### 15.1 Create the bot and get credentials

1. On Telegram, message `@BotFather`. `/newbot`, follow prompts.
   Save the bot token (looks like `123456:ABC-...`).
2. On Telegram, message `@userinfobot` to get your numeric user
   ID.

### 15.2 Fill in `secrets.yaml`

On the **Hub**, edit `~/.fitt/secrets.yaml` and uncomment the
Telegram block:

```yaml
telegram:
  bot_token: 123456:ABC-xxxxxxxxxxxxx
  allowlist_user_ids:
    - 123456789
```

### 15.3 Install the bot service

From an **elevated** PowerShell on the Hub at the repo root:

```powershell
.\scripts\install-telegram-bot.ps1 -SetupVenv
```

This runs `uv sync` in `telegram-bot\` to create its venv,
verifies the bot can `import fitt_telegram_bot`, and registers
`FITTTelegramBot` as a Windows service with auto-start.

No firewall rule is needed - the bot makes outbound connections
only (Telegram Bot API + localhost gateway).

Verify:

```powershell
Get-Service FITTTelegramBot
Get-Content "$env:USERPROFILE\.fitt\logs\telegram-bot.stdout.log" -Tail 20
```

Then open Telegram on your phone, find your bot, send `/start`.
You should get a welcome message. If nothing happens, check:

1. Is your Telegram user id in `allowlist_user_ids`? Non-
   allowlisted users are silently dropped by design.
2. Is the bot token correct? `@BotFather` -> `/mybots` -> your bot
   -> **API Token**.
3. Is the gateway healthy
   (`curl http://localhost:8080/health`)?

## Step 16 - Open WebUI (optional)

Open WebUI is a browser chat UI that also runs on the **Hub**. It
needs Docker Desktop.

### 16.1 Install Docker Desktop (if not already)

```powershell
winget install --id=Docker.DockerDesktop -e
# Start Docker Desktop and sign in if prompted.
```

### 16.2 Install Open WebUI

On the **Hub**, elevated PowerShell at the repo root:

```powershell
.\scripts\install-open-webui.ps1
```

This reads your Bearer token from `secrets.yaml`, writes a
gitignored `.env` next to `docker-compose.yml`, brings up the
container, and adds a Tailscale-scoped firewall rule for TCP
3000.

Verify from the Hub:

```
http://localhost:3000/
```

Or from your phone on Tailscale:

```
http://<hub-tailscale-ip>:3000/
```

First visit shows the admin signup. Create your admin account.
The container then disables further signups (`ENABLE_SIGNUP=false`),
so random visitors on your tailnet cannot register.

In Open WebUI, pick any of the FITT aliases (`fitt-default`,
`fitt-smart`, ...) from the model dropdown. Responses flow
through the gateway, so they show up in `fitt cost` alongside
your Telegram traffic.

---

## Resilience checks (5 min)

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

## You're done

What you have now:

- A Windows service on the Hub that routes chat requests between
  local satellites and the cloud.
- IDE chat on any laptop that can reach the Hub over Tailscale.
- Optional Telegram bot and Open WebUI sharing the same gateway
  and cost log.
- One Bearer token, one config, swappable models.
- Logs in `~/.fitt/logs/gateway.log`.
- Cost visibility with `uv run fitt cost`.

Next: live with it for a week or two before starting the next
phase - see [`../FITT_ROADMAP.md`](../FITT_ROADMAP.md) guiding
principle 9.

---

## Troubleshooting

`gateway/README.md` has a dedicated troubleshooting section: auth
401, `/ready` 503, streaming cost=0, service crash loops, firewall
issues, update workflow.

## Common slip-ups

- **`OLLAMA_HOST=0.0.0.0`** not taking effect on the satellite:
  you changed the env var but didn't restart Ollama.
- **Firewall rule on Public profile**: Windows marked your
  Tailscale interface as Public. Settings -> Network -> Tailscale
  -> Private.
- **Trailing whitespace in the Bearer token**: copy-paste ate a
  space. Regenerate with the one-liner in 5.3.
- **Wrong Tailscale IP in `config.yaml`**: your satellite got a
  new IP after sleep/wake. Use MagicDNS (hostnames) if this
  happens often.
- **`uv sync` fails with network errors**: uv downloads Python +
  deps from the internet; corporate proxies or flaky Wi-Fi can
  trip it up. Retry, or set `UV_HTTP_TIMEOUT=120`.
- **`install-service.ps1` reports "Expected Python at ... \gateway\.venv\..."**:
  run `uv sync` in `gateway\` first, or re-run the script with
  `-SetupVenv`.
