# FITT Quickstart

> Start to finish: from nothing to a working hub reachable from
> your laptop's IDE, your phone's browser, and the Telegram app.
> Expect ~30 minutes on a hub that has Docker and Tailscale
> installed, plus whatever time it takes to pull your first
> local model.

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

One machine, always on. Desktop, mini-PC, or NAS with Container
Station / Docker all work. The Hub hosts the gateway, the
Telegram bot, and Open WebUI - all as containers under one
`docker compose` command.

## Step 1 - Tailscale on the Hub (3 min)

On Windows:

```powershell
winget install --id=tailscale.tailscale -e
```

On macOS:

```bash
brew install --cask tailscale      # or install from the Mac App Store
```

On Linux:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

On a QNAP: install Tailscale from the QNAP App Center.

Sign in. Verify (any platform):

```bash
tailscale status
```

Note the Hub's `100.x.x.x` IP. You'll hand it to clients in
Part C.

Tailscale's default ACL already allows every device on your
tailnet to reach every other device. Don't tighten ACLs unless
you know exactly which ports to allow.

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

## Step 3 - Install Docker (5 min)

Docker runs the three FITT services as containers. You do not need
to install Python or uv on the hub; the images bring everything
they need.

### 3.1 Pick your platform

Container Station on QNAP bundles Docker, the `docker` CLI, and
`docker compose`. Docker Desktop on macOS/Windows and Docker Engine
on Linux all give you the same three pieces. You can use the GUI,
SSH in and use the CLI, or mix the two - they manage the same
containers.

On Windows:

```powershell
winget install --id=Docker.DockerDesktop -e
```

On macOS:

```bash
brew install --cask docker       # or install Docker Desktop from docker.com
```

On Linux (Engine, no GUI needed for a server):

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"    # log out + back in for this to take effect
```

On QNAP: install Container Station from the QNAP App Center. You
likely have it if you run Jellyfin, Plex, or similar.

### 3.2 Clone the repo on the hub

Pick anywhere that suits your hub's filesystem conventions. On a
QNAP, SSH in and clone under `/share/Public/` or wherever you
keep "my own" files (not the same folder as FITT's runtime data -
see Step 5).

```bash
cd /share/Public
git clone https://github.com/qam4/home-ai-cluster.git
```

On Linux/macOS/Windows, any directory you'd normally keep code
in is fine.

Git is pre-installed on macOS, most Linux distros, and most
Container Station QNAP hosts. If you need it on Windows:

```powershell
winget install --id=Git.Git -e
```

**QNAP note — if `git: command not found` in your SSH shell:**
QNAP's minimal SSH shell on older QTS versions ships without
`git`. Two ways around it:

- **Clone elsewhere, copy the repo to the NAS.** Simplest. On
  your laptop, `git clone`, then `scp -r home-ai-cluster admin@<nas>:/share/Public/`
  (or drag-and-drop in File Station). You lose `git pull` for
  updates; to update, clone + copy again, or rsync.
- **Install Entware + git on the NAS.** Entware is a QPKG
  package manager available in App Center. After installing it:
  `ssh admin@<nas>; /opt/bin/opkg update; /opt/bin/opkg install git git-http`.
  (`git-http` is needed for HTTPS clones against GitHub.)

## Step 4 - (Optional) Small Ollama fallback on the Hub (5 min)

Lets the Hub answer local-model requests when every satellite is
asleep. **Skip if the Hub has no GPU**, which is the normal case
for a NAS.

If you do want it, install Ollama natively on the Hub (not in
Docker - GPU passthrough to Docker is more trouble than it's worth
for satellite-on-hub duty), set `OLLAMA_HOST=0.0.0.0`, and pull a
small model.

The default `config.example.yaml` uses `qwen-coder-small` as the
fallback for `fitt-default`. If you skip this step, edit
`config.yaml` in Step 5.2 to remove the `fallback:` line and the
`qwen-coder-small` block.

## Step 5 - Config and secrets (10 min)

Everything FITT reads and writes at runtime lives under one
directory on the hub: `$FITT_HOME`. Config, secrets, memory,
session history, logs, and Open WebUI's state all live there.
Snapshot that folder, you have the whole hub's state.

Pick a path that matches your NAS's app-data convention. On a
QNAP, mirror where Jellyfin/Plex store their config:

```bash
# QNAP example: /share/Public/fitt
# Linux server: /srv/fitt
# macOS:        /Users/you/.fitt
FITT_HOME=/share/Public/fitt
```

### 5.1 Seed `$FITT_HOME`

On the **Hub**, over SSH:

```bash
mkdir -p "$FITT_HOME"
cd /path/to/home-ai-cluster      # wherever you cloned in Step 3.2
cp configs/config.example.yaml  "$FITT_HOME/config.yaml"
cp configs/secrets.example.yaml "$FITT_HOME/secrets.yaml"
cp .env.example                 "$FITT_HOME/../.env"   # next to docker-compose.yml; see below
```

Actually, the `.env` goes in the repo root (next to
`docker-compose.yml`), not under `$FITT_HOME`. Adjust the last
command to your clone path:

```bash
cp .env.example .env        # in the home-ai-cluster repo root
```

**QNAP note — SSH as admin on first setup:** Non-admin QNAP
accounts (the ones you'd SSH in as for daily use) typically
can't create directories under `/share/Public/` or write to a
freshly-created `$FITT_HOME`. Permission denied on `mkdir` or
`cp` usually means this. Either `ssh admin@<nas>` for the
initial setup, or `chown -R <your-user>:everyone $FITT_HOME` as
admin once and then work as your regular user from there.

### 5.2 Edit `$FITT_HOME/config.yaml`

Replace the `100.x.y.z` placeholder for `qwen-coder-big` with the
**LAN IP** of the satellite you'll set up in Part B (tailnet IP
works too, but only needed if the satellite lives off your home
network). If you don't have a satellite yet, leave the
placeholder — the gateway will start, the cloud alias will work,
and `/ready` will return 503 until you wire the satellite up.

If the hub has no local Ollama fallback (Step 4 skipped), remove
the `qwen-coder-small` model block and the `fallback:` line on
`qwen-coder-big`.

### 5.3 Generate a Bearer token

On Windows (PowerShell):

```powershell
[Convert]::ToBase64String([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
```

On macOS / Linux / QNAP:

```bash
openssl rand -base64 32
```

Copy the string that prints. You'll paste it into two places:
`$FITT_HOME/secrets.yaml` and the repo root `.env`.

### 5.4 Edit `$FITT_HOME/secrets.yaml`

- Bearer token from Step 5.3 into `allowed_tokens[0].token`.
- OpenRouter key from Step 2 into `openrouter_api_key`.

Leave the Anthropic, `api_keys`, and Telegram blocks commented
out for v0.

### 5.5 Lock down permissions

The gateway refuses to load `secrets.yaml` if it's
group/world-readable. Fix it once:

```bash
chmod 0600 "$FITT_HOME/secrets.yaml"
```

### 5.6 Edit the repo root `.env`

Compose reads this file for `FITT_HOME`, `PUID`, `PGID`, `TZ`,
and the Bearer token it needs to inject into Open WebUI.

```bash
# home-ai-cluster/.env
FITT_HOME=/share/Public/fitt
PUID=1000
PGID=1000
TZ=America/Los_Angeles
FITT_BEARER_TOKEN=<paste the same token from Step 5.3>
```

`PUID` and `PGID` should match the host user that owns
`$FITT_HOME`. On QNAP the default admin user is `1000:1000`; on
Linux, `id -u` and `id -g` tell you.

## Step 6 - Bring up the hub

Two equivalent flavors (6.A GUI, 6.B CLI). Pick the one that
matches how you normally manage containers on this box; both
talk to the same Docker daemon and produce the same running
containers. 6.C covers an optional advanced workflow for driving
a headless hub from your laptop.

### 6.A - Container Station (QNAP, GUI)

**⚠️ Important caveat up front:** Container Station's compose
importer **does not read the repo's `.env` file**. Every variable
in `docker-compose.yml` that looks like `${FOO:-default}` becomes
the default (or fails if it's required), regardless of what you
typed into `.env`. If you need a custom `FITT_HOME`, `TZ`,
`FITT_PORT`, or `FITT_BEARER_TOKEN` (you will — the bearer token
is required), you'd have to inline them into the YAML you paste.
**Prefer 6.B for anything beyond a default install.**

If you still want the GUI path:

1. Container Station -> **Applications** -> **Create**.
2. Give the application a name (`fitt` is fine).
3. Paste the contents of `docker-compose.yml` from the repo into
   the YAML editor. Hardcode the values `.env` would have
   supplied — at minimum `FITT_HOME`, `FITT_BEARER_TOKEN`, and
   `PUID`/`PGID`.
4. Click **Create**. Container Station pulls the Open WebUI image,
   builds the gateway and bot images on the NAS, creates the
   Docker network, and starts all three containers.
5. Wait ~60 seconds. The application dashboard shows
   `fitt-gateway`, `fitt-telegram-bot`, and `fitt-open-webui` in
   "Running" state.

If Container Station rejects the compose file with an error
mentioning `depends_on` or `condition`, your QTS version doesn't
support compose's health conditions. Replace the two
`depends_on:` blocks in the compose file with the simpler list
form (`depends_on: [gateway]`) and try again. The bot and Open
WebUI retry their gateway connections anyway.

### 6.B - SSH + docker compose

From the repo root on the hub:

```bash
docker compose up -d
```

That's it. Images build (gateway + telegram-bot) and pull
(open-webui), network comes up, all three containers start.

To watch logs:

```bash
docker compose logs -f gateway
```

### 6.C - (Optional) Drive the hub from your laptop

If the hub is awkward to edit on directly (no good editor, no
VS Code Remote, minimal shell), you can use Docker's remote-host
mode: edit and `docker compose` on your laptop, build and run on
the hub.

```powershell
# Windows (PowerShell); on macOS/Linux use: export DOCKER_HOST=ssh://...
$env:DOCKER_HOST = "ssh://admin@<hub-tailscale-ip>"
docker version       # Client: your laptop; Server: the hub
```

With `DOCKER_HOST` set, every `docker compose up -d`,
`docker compose logs`, `docker ps` from your laptop terminal
targets the hub's daemon instead of the laptop's. The compose
file and source tree stay on the laptop; build context is shipped
to the hub over SSH, and containers run on the hub.

Build still happens on the hub (so a NAS is still the
bottleneck for compile time), but editing, pushing changes, and
tailing logs are all instant. Containers started this way are
fully visible in the hub's Container Station / Docker Desktop
GUI - same daemon, same containers.

## Step 7 - Verify the gateway (2 min)

The gateway defaults to port **8421** (not 8080 — that port collides
with QNAP's admin web UI, Tomcat, and several other common services).
If you need a different port — say, because 8421 is also taken on
your hub — edit `FITT_PORT` and `GATEWAY_HOST_PORT` in `.env`. The
two values can differ for bridge networking (the former is the
container-internal port, the latter what the host publishes); keep
them in sync if you switch to `network_mode: host`.

From any device on Tailscale (or the hub itself):

```bash
curl http://<hub-tailscale-ip>:8421/health           # {"status":"ok"}
curl http://<hub-tailscale-ip>:8421/v1/models        # JSON listing your aliases
```

**First boot takes 15-30 seconds** while Python imports LiteLLM
and friends. If `/health` doesn't respond immediately, wait and
retry. Docker restarts the container if it actually crashed (our
`restart: unless-stopped` policy).

`/ready` (as opposed to `/health`) returns 503 until every alias
has at least one reachable backend. That's expected if your
satellite isn't up yet - move to Part B.

### Troubleshooting first boot

If something looks wrong, the first thing to do is read what the
gateway actually said. The gateway is configured with
`restart: on-failure:3` (intentional, see below), so a startup
failure will:

1. Try to start three times.
2. Stop. `docker compose ps` shows `fitt-gateway` as `Exited (N)`,
   not `unhealthy` or `Restarting`. The exit code tells you the
   failure class:
   - **Exited (2)** = config error (bad YAML, missing field,
     wrong type). Fix `$FITT_HOME/config.yaml` or
     `$FITT_HOME/secrets.yaml`.
   - **Exited (1) or other non-zero** = unexpected crash (Python
     import error, missing dependency, runtime exception during
     startup). The cause is in the logs.
3. Sit there until you fix the underlying issue and run
   `docker compose up -d` again, or `docker compose restart
   gateway` after the fix.

To see the actual error message:

```bash
docker compose logs fitt-gateway --tail 30
```

The gateway prints a friendly explanation to stderr before
exiting; that's what you'll see. For example, on a bad
`secrets.yaml`:

```
============================================================
[fitt-gateway] CONFIG ERROR (exit 2): /fitt/secrets.yaml
failed validation: api_keys must be a YAML mapping (key: value
pairs), not a list...
============================================================
[fitt-gateway] To fix: edit your config files in ~/.fitt/...
```

**Why `on-failure:3`, not `unless-stopped`?** A normal `restart:
unless-stopped` would loop forever on a bad config, with the
healthcheck eventually marking the container `unhealthy` —
hiding the real error in `docker logs`. With `on-failure:3` the
container exits visibly after three tries, so `compose ps` is
honest about WHY it stopped. If a runtime issue genuinely needs
recovery beyond three restarts, that's a sign something deeper
is wrong; it's not a footgun, it's a feature.

If the failure isn't covered by the log:

```bash
scripts/smoke-compose.sh
```

It builds the gateway image, starts it against a throwaway
config, hits `/health` and `/v1/models`, and tears down. If that
passes, the image is fine and the issue is in your
`$FITT_HOME/config.yaml`, `secrets.yaml`, or `.env`. If it fails,
the build or the entrypoint is broken — read the compose logs
for the real error.

Other useful one-liners:

```bash
docker compose ps                           # container states
docker compose logs fitt-gateway --tail 50  # gateway stdout/stderr
docker compose logs fitt-telegram-bot       # bot stdout/stderr
docker compose logs -f -t                   # tail all three live
tail -f "$FITT_HOME/logs/gateway.log" \
        "$FITT_HOME/logs/telegram-bot.log"  # structured JSON on disk
```

---

# Part B — Satellites

Any machine (Hub included, see Step 4) that hosts local models. You
can have zero, one, or many. Each satellite contributes one or more
models identified by their Ollama tag.

Do this section **once per satellite**.

## Step 8 - Tailscale on the satellite (3 min)

Same install as Step 1, on the satellite machine this time.

On Windows:

```powershell
winget install --id=tailscale.tailscale -e
```

On macOS:

```bash
brew install --cask tailscale
```

On Linux:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Sign in on the same Tailscale account as the Hub. Verify:

```bash
tailscale status
```

Note this satellite's `100.x.x.x` IP. You'll use it in step 11.

## Step 9 - Ollama on the satellite (5 min)

Install Ollama.

On Windows:

```powershell
winget install --id=Ollama.Ollama -e
```

On macOS:

```bash
brew install --cask ollama       # or download from ollama.com
```

On Linux:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Then:

1. Set `OLLAMA_HOST=0.0.0.0` in the machine's environment.
   - Windows: add it as a **User variable** in the system env
     vars dialog.
   - macOS/Linux: add `export OLLAMA_HOST=0.0.0.0` to your
     shell rc file, or `launchctl setenv` on macOS to make it
     visible to GUI apps.

   Without this, the Hub can't reach Ollama from another
   machine; Ollama defaults to listening on loopback only.
2. Restart Ollama so it picks up the env var (quit from the tray
   on Windows/macOS, or `systemctl restart ollama` on Linux).

Verify from the **Hub**:

```bash
# Use the satellite's LAN IP if it shares a subnet with the hub
# (most home setups). Fall back to the tailnet IP if the
# satellite lives elsewhere.
curl http://<satellite-lan-ip>:11434/api/tags
```

JSON response = good. Connection refused = the env var didn't
take (most common cause: didn't restart Ollama after setting it).
Timeout from the **hub shell** but success from a **laptop on
the tailnet** usually means the hub isn't actually on the tailnet
for outbound traffic (common on QNAPs with Tailscale installed as
a Docker container rather than a QPKG — see troubleshooting below).

## Step 10 - Pull a model (time varies)

On the satellite, pull whatever model this machine is big enough
to serve. The command is the same on every platform:

```bash
ollama pull qwen2.5-coder:14b      # example for 12-16 GB VRAM
```

Pick the tag based on the VRAM you have. Bigger models = better
answers but slower and VRAM-hungry. The tag you use here is the
`model:` value you'll write into `config.yaml` next.

## Step 11 - Wire the satellite into the Hub

On the **Hub**, edit `$FITT_HOME/config.yaml` with your editor
of choice (over SSH on a NAS, or on a shared mount):

```bash
# Linux/macOS/QNAP over SSH:
nano "$FITT_HOME/config.yaml"

# Windows + Docker Desktop:
notepad $env:FITT_HOME\config.yaml
```

Add (or update) a model entry pointing at this satellite:

```yaml
models:
  - id: qwen-coder-big             # unique within this file
    backend: ollama
    endpoint: http://<satellite-lan-ip>:11434
    model: qwen2.5-coder:14b       # exactly the ollama tag from step 10
    fallback: qwen-coder-small     # optional; another model id in this file
```

**Which endpoint address should you use?** Three good options,
in rough order of complexity. Pick the one that matches your
failure-mode priorities.

1. **LAN IP (simplest).** `http://192.168.1.50:11434`. Packet
   stays on the home LAN, doesn't depend on Tailscale to route,
   and keeps working even if Tailscale is down. Downside: an IP
   can change when the satellite wakes from sleep unless you
   reserve it on the router. Best default when the hub and
   satellite share a network.
2. **Tailnet hostname.** `http://laptop.ts-name.ts.net:11434` or
   the short MagicDNS name. Stable across sleep/wake, reboots,
   even moving a satellite between home and office. Needs
   Tailscale up on both hub and satellite, and the gateway's
   Docker bridge DNS pointing at `100.100.100.100` (the default
   compose file does this). Use when satellites roam or when
   you prefer names over IP bookkeeping.
3. **Tailnet raw IP (`100.x.y.z`).** Splits the difference:
   stable hostnames' benefit without the DNS dependency. IPs on
   the tailnet are assigned once per device and don't drift
   unless you deliberately rotate them.

**The tradeoff: "everything needs Tailscale" vs "everything
needs stable LAN."** Pick whichever failure you'd rather handle:

- Tailnet names/IPs mean the hub can't reach satellites when
  Tailscale has a bad day. (Rare. Personal experience: a handful
  of minutes per year.)
- LAN IPs mean an unexpected IP change takes satellites offline
  until you notice and update config.yaml.

**Want both?** The alias chain gives you free fallback. Define
two models for the same backend — one using the tailnet name
and one using the LAN IP — then make the second the fallback
for the first:

```yaml
models:
  - id: qwen-coder-big
    backend: ollama
    endpoint: http://laptop.your-tailnet.ts.net:11434
    model: qwen2.5-coder:14b
    fallback: qwen-coder-big-lan
  - id: qwen-coder-big-lan
    backend: ollama
    endpoint: http://192.168.1.50:11434
    model: qwen2.5-coder:14b
```

When the primary's DNS or route fails, the router automatically
retries the LAN endpoint. Belt-and-suspenders, zero code.

**If you go with a LAN IP**, set a DHCP reservation on your
router so the IP doesn't drift, or use an mDNS hostname like
`http://<laptop-hostname>.local:11434` when your router serves
one.

Then make sure at least one alias points at it:

```yaml
aliases:
  fitt-default: qwen-coder-big     # everyday coding (this satellite)
```

Save. Restart the gateway so it picks up the new config:

```bash
docker compose restart gateway
```

Verify from the Hub:

```bash
docker compose exec gateway fitt status    # should show the satellite as reachable
curl http://localhost:8421/ready           # should return 200 once every alias has a live backend
```

If `/ready` still returns 503, check the gateway logs:

```bash
docker compose logs --tail=50 gateway
```

They'll name the alias that failed and why.

## Adding another satellite later

Want to add a second machine (work laptop, second desktop,
someone else's box)? Just repeat **Part B** on it:

1. Tailscale (step 8).
2. Ollama + `OLLAMA_HOST=0.0.0.0` (step 9).
3. `ollama pull <some-tag>` (step 10).
4. On the Hub, add a new model block to `config.yaml` with a new
   `id`, the new satellite's IP, and the tag you pulled. Bind it
   to an alias (or use it as another model's `fallback`).
5. `docker compose restart gateway` on the hub.

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

Install Tailscale and sign in on the same account as the Hub.

On Windows:

```powershell
winget install --id=tailscale.tailscale -e
```

On macOS:

```bash
brew install --cask tailscale
```

On Linux:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

On phones: install the Tailscale app from the App Store or Play
Store.

Verify:

```bash
tailscale status
```

## Step 13 - VS Code + Continue (5 min)

On your laptop (the machine where you want IDE chat).

### 13.1 Install VS Code and Continue

If you don't already have VS Code:

On Windows:

```powershell
winget install --id=Microsoft.VisualStudioCode -e
```

On macOS:

```bash
brew install --cask visual-studio-code
```

On Linux: use your distro's package manager, or download from
[code.visualstudio.com](https://code.visualstudio.com).

Then install the **Continue** extension from the VS Code
marketplace (sidebar -> Extensions -> search "Continue" ->
Install).

### 13.2 Add FITT aliases to Continue's config.yaml

Continue stores its configuration at
`%USERPROFILE%\.continue\config.yaml` on Windows
(`~/.continue/config.yaml` on POSIX). Open it in your editor and
add two model entries under `models:`:

```yaml
models:
  - name: FITT smart (Claude via OpenRouter)
    provider: openai
    apiBase: http://<hub-tailscale-ip>:8421/v1
    apiKey: <your-bearer-token-from-5.3>
    model: fitt-smart
    capabilities:
      - tool_use        # enables Continue's Agent/Plan modes
    roles:
      - chat
      - edit
      - apply

  - name: FITT default (local Qwen)
    provider: openai
    apiBase: http://<hub-tailscale-ip>:8421/v1
    apiKey: <your-bearer-token-from-5.3>
    model: fitt-default
    capabilities:
      - tool_use        # see note below
    roles:
      - chat
      - edit
      - apply
```

The `capabilities: [tool_use]` line matters. Continue auto-detects
tool support from well-known provider/model pairs (e.g. direct
Anthropic Claude); with `provider: openai` + a custom `apiBase`
it can't tell, and Agent/Plan modes show as "unavailable." Adding
the capability explicitly unlocks them.

On smaller local models (Qwen 2.5-Coder at 14B and below), tool
calling is possible but less reliable. If Agent mode behaves
oddly with `fitt-default`, fall back to Chat mode for that alias
or remove its `tool_use` capability.

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
http://<hub-tailscale-ip>:8421/v1/models
```

Should return JSON. If you can see this from the phone, the full
stack is working.

From your **phone on mobile data** (off the tailnet), try:

```
http://<hub-public-ip>:8421/
```

Should **fail** to connect. If it succeeds, the firewall rule is
wrong - revisit step 6.

## Step 15 - Telegram bot (optional)

The bot's container was already started in Step 6 as part of
`docker compose up -d`. But it sits idle until you give it a bot
token. Enabling it is two edits and one restart.

### 15.1 Create the bot and get credentials

1. On Telegram, message `@BotFather`. `/newbot`, follow prompts.
   Save the bot token (looks like `123456:ABC-...`).
2. On Telegram, message `@userinfobot` to get your numeric user
   ID.

### 15.2 Fill in `secrets.yaml`

On the **Hub**, edit `$FITT_HOME/secrets.yaml` and uncomment the
Telegram block:

```yaml
telegram:
  bot_token: 123456:ABC-xxxxxxxxxxxxx
  allowlist_user_ids:
    - 123456789
```

**Two different tokens, don't confuse them.** The `bot_token`
above is from `@BotFather` and authenticates the bot *to
Telegram*. It's distinct from the Bearer token in
`allowed_tokens:` at the top of the file, which authenticates
the bot *to the gateway*.

**About the `client:` tag on the gateway token.** The bot sends
`X-FITT-Client: telegram` on every request, so approval routing
works even if you leave `client:` unset on the gateway token.
If you want the config to be self-describing (recommended when
you start running multiple interfaces), give the bot its own
gateway token with `client: telegram` — see
`configs/secrets.example.yaml` for the pattern.

### 15.3 Restart the bot

From the repo root on the hub:

```bash
docker compose restart telegram-bot
docker compose logs -f telegram-bot
```

Look for a line like `telegram_bot.started`. Then open Telegram
on your phone, find your bot, send `/start`. You should get a
welcome message.

If nothing happens, check:

1. Is your Telegram user id in `allowlist_user_ids`?
   Non-allowlisted users are silently dropped by design.
2. Is the bot token correct? `@BotFather` -> `/mybots` -> your
   bot -> **API Token**.
3. Is the gateway healthy? `docker compose ps` - `fitt-gateway`
   should show `healthy`.

## Step 16 - Open WebUI (optional)

Open WebUI was also started by `docker compose up -d` in Step 6.
All that's left is to create the admin account and close signup.

Verify from the Hub:

```
http://localhost:3000/
```

Or from your phone on Tailscale:

```
http://<hub-tailscale-ip>:3000/
```

**Bootstrap steps (once per fresh install):**

1. First visit shows the signup form. Fill it in - the first
   account becomes the admin automatically.
2. After signing in, go to **Admin Panel -> Settings -> General**
   and turn **Enable Signup** off. Save.

The door is now closed: other tailnet members can't self-register,
but you stay logged in.

**Why the manual step?** `ENABLE_SIGNUP` is a *PersistentConfig*
variable in Open WebUI — its first-boot value seeds the database,
and changes made after that come from the Admin UI (or a database
edit). The compose file defaults it to `true` so the admin can
actually be created on first boot. If the first account couldn't
sign up, the instance would be unreachable with no way in short
of deleting the volume. See the troubleshooting section below if
you ended up stuck that way.

In Open WebUI, pick any of the FITT aliases (`fitt-default`,
`fitt-smart`, ...) from the model dropdown. Responses flow
through the gateway, so they show up in `fitt cost` alongside
your Telegram traffic.

### Troubleshooting: locked out of Open WebUI

Symptom: you set `ENABLE_SIGNUP=false` in the compose override
before creating the first admin, so the signup form is gone and
no user exists to log in with. Open WebUI stores this value in
its SQLite DB on first boot; flipping it in `docker-compose.yml`
has no effect on subsequent starts.

Fastest recovery (wipes Open WebUI accounts and chats; safe on a
fresh install — this directory only holds Open WebUI state, not
the FITT gateway's):

```bash
docker compose stop open-webui
rm -rf "$FITT_HOME/open-webui"
docker compose up -d open-webui
```

Or edit the value in place without losing data:

```bash
docker exec -it fitt-open-webui \
  sqlite3 /app/backend/data/webui.db \
  "UPDATE config SET data = json_set(data, '\$.ENABLE_SIGNUP', json('true'));"
docker compose restart open-webui
```

Then redo the two bootstrap steps above.

---

# Part D — Wire up a project (optional; needed for tool-capable chat)

Everything up to here gives you a *chat* hub: the LLM talks, you
reply. To let the LLM **read code and run commands** on your
behalf, you register a *project* and give the gateway SSH access
to the machine where the code actually lives.

A project is a logical workspace: a name + a filesystem path +
optionally an SSH host. Tools that touch files (`read_file`,
`grep_repo`, `git_status`, etc.) take a project argument, look up
the entry, and dispatch to the execution host.

Three universal steps (1-3), then an OS-specific step (4) for how
the satellite accepts the gateway's public key.

### What actually runs, end to end

Useful to have in mind before you start debugging.

1. The gateway container runs:
   `ssh -i /fitt/ssh/id_ed25519 <user>@<host> "<command>"`
2. The satellite's sshd receives the connection and checks
   `authorized_keys` for a matching public key.
3. sshd launches the remote **login shell** and feeds it
   `"<command>"` as a single string.
4. The shell resolves the PATH, finds the binary, runs it.

Step 3 is where most surprises live. Which shell sshd picks
depends on the satellite's OS:

- **Linux/macOS**: the user's login shell from `/etc/passwd`
  (change with `chsh -s /bin/bash` on the satellite).
- **Windows**: whatever `HKLM:\SOFTWARE\OpenSSH\DefaultShell`
  points at, or `cmd.exe` if unset. cmd.exe doesn't resolve
  POSIX tools like `cat`, `uname`, `grep`, so every FITT tool
  that shells out will fail with `command not found`.

There are two levers to force a specific shell:

- **Satellite-side**: change the default shell (below, step 18).
  OS-native, applies to every SSH session on that machine.
  `chsh` on POSIX, `DefaultShell` registry key on Windows.
- **Per-project override on the hub** (later, if you need it):
  FITT will support pinning a specific shell in the project
  record so the backend wraps the command with that shell. Not
  wired in today; today's doc covers the satellite-side lever.

## Step 17 - Read the gateway's public key

The gateway generates its own SSH identity on first boot. No
ssh-keygen on your part.

On the hub:

```bash
docker compose exec gateway fitt ssh pubkey
```

Prints a single line starting with `ssh-ed25519 AAAA... fitt-gateway`.
Copy it; you'll paste it on the satellite in step 20.

If this is a fresh install and the key doesn't exist yet, the
command generates it on first run. Calling it a second time
prints the same key — the gateway never rotates the key silently.

## Step 18 - Install and enable SSH on the satellite

The satellite is whatever machine holds the code you want FITT to
read: your laptop, a desktop, a cloud dev box. OS-specific below.

### 18.a — Linux / macOS satellite

Usually already running. Check from the satellite:

```bash
sudo systemctl status ssh      # Linux (systemd)
sudo launchctl list | grep ssh # macOS
```

If SSH isn't running, on Linux: `sudo systemctl enable --now ssh`.
On macOS: System Settings → General → Sharing → Remote Login → On.

If your login shell isn't bash/zsh and you want a different one
for FITT's sessions, run `chsh -s /bin/bash` on the satellite.
Changes the shell for every SSH session on that machine, not
just FITT's.

### 18.b — Windows satellite with Git Bash

Windows is the trickiest path. Three things have to line up:
OpenSSH Server installed, default shell set to a POSIX shell
(so tools like `cat`, `ls`, `grep` work), and the public key
placed where Windows' sshd actually looks for admin accounts.

FITT tools assume POSIX semantics. On Windows that means either
Git Bash (recommended — tight install, no VM) or WSL (heavier
but more complete; see 18.c). **Don't use cmd.exe or PowerShell
as the default shell** — FITT tools will fail.

**Install OpenSSH Server.** In an admin PowerShell:

```powershell
Add-WindowsCapability -Online -Name "OpenSSH.Server~~~~0.0.1.0"
```

If `Get-WindowsCapability -Online -Name "OpenSSH.Server~~~~0.0.1.0"`
reports `State : InstallPending`, **reboot** for the install to
complete.

Then start and enable sshd:

```powershell
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
Get-Service sshd                 # Status: Running
```

**Point sshd at Git Bash as the default shell.** Still in admin
PowerShell, first confirm where bash lives:

```powershell
Get-Command bash | Format-List Source
# Accept only if this is a Git Bash installation
# (NOT C:\Windows\System32\bash.exe, which is WSL).
```

If you have both Git Bash and WSL installed, `Get-Command bash`
will resolve to whichever appears first on PATH — often WSL.
Don't rely on it. Pick the explicit path to Git Bash's bash.exe
(usually `C:\Program Files\Git\usr\bin\bash.exe` or
`C:\Tools\Git\usr\bin\bash.exe`) and register that.

```powershell
$bash = "C:\Tools\Git\usr\bin\bash.exe"   # adjust to your install
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" `
    -Name DefaultShell -Value $bash -PropertyType String -Force
# `-lc` invokes bash as a login shell. Without it, PATH is
# minimal and `uname`, `cat`, `ls` don't resolve when sshd
# runs a remote command non-interactively.
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" `
    -Name DefaultShellCommandOption -Value "-lc" `
    -PropertyType String -Force
Restart-Service sshd
```

Verify:

```powershell
Get-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" |
    Select-Object DefaultShell, DefaultShellCommandOption
```

### 18.c — Windows satellite via WSL

If you have WSL installed and your repo lives inside the WSL
distro (`/home/you/src/...`), SSH to that distro directly and
skip Git Bash entirely. Treat it as a Linux satellite (18.a).
Cleaner if you can.

## Step 19 - Confirm port 22 is reachable from the hub

From the hub shell (not inside the gateway container):

```bash
nc -zv <satellite-hostname> 22
# or
curl -v telnet://<satellite-hostname>:22 2>&1 | grep Connected
```

If it connects, sshd is running and reachable. If not, check the
satellite's firewall rules for inbound 22 (Windows auto-creates
`OpenSSH-Server-In-TCP` during install — confirm it's Enabled).

## Step 20 - Authorise the gateway's public key on the satellite

Different location depending on the satellite's OS and user type.

### 20.a — Linux / macOS satellite

In the satellite's shell:

```bash
mkdir -p ~/.ssh && chmod 0700 ~/.ssh
echo "<paste the pubkey from step 17 here>" >> ~/.ssh/authorized_keys
chmod 0600 ~/.ssh/authorized_keys
```

### 20.b — Windows satellite, regular user account

```powershell
# PowerShell as the user whose account you'll SSH as
$keyPath = "$env:USERPROFILE\.ssh\authorized_keys"
if (-not (Test-Path $keyPath)) {
    New-Item -ItemType File -Path $keyPath -Force | Out-Null
}
notepad $keyPath
# Paste the pubkey as one line, save, close.

# Tighten ACLs so sshd accepts the file.
$acl = Get-Acl $keyPath
$acl.SetAccessRuleProtection($true, $false)
$acl.Access | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    "$env:USERNAME", "FullControl", "Allow")
$acl.AddAccessRule($rule)
Set-Acl $keyPath $acl
```

### 20.c — Windows satellite, Administrator account

Admin accounts on Windows bypass `~/.ssh/authorized_keys`. Use
`C:\ProgramData\ssh\administrators_authorized_keys` instead.

Check if you're an admin:

```powershell
net localgroup administrators
# If your username appears, this section applies to you.
```

Then, in **admin PowerShell**:

```powershell
$keyPath = "C:\ProgramData\ssh\administrators_authorized_keys"
if (-not (Test-Path $keyPath)) {
    New-Item -ItemType File -Path $keyPath -Force | Out-Null
}
notepad $keyPath
# Paste the pubkey from step 17 as one line, save, close.

# ACLs for administrators_authorized_keys: Administrators + SYSTEM only.
icacls $keyPath /inheritance:r
icacls $keyPath /grant "Administrators:F" "SYSTEM:F"
icacls $keyPath /remove "Authenticated Users" "BUILTIN\Users" 2>$null
icacls $keyPath
# The final output should list ONLY Administrators and SYSTEM.
```

## Step 21 - Test the SSH handshake

Back on the hub:

```bash
docker compose exec gateway fitt ssh test \
    <user>@<satellite-hostname> \
    --command "uname -a && pwd"
```

Expected: a line containing the satellite's uname output, then
the remote home directory, then `ok`. If the command errors, the
stderr is printed verbatim so you can debug. Common failures:

| symptom | likely cause |
|---|---|
| `Permission denied (publickey)` | key isn't in the right `authorized_keys`, or ACLs on the keys file are too permissive |
| `Host key verification failed` | extremely rare; delete `$FITT_HOME/ssh/known_hosts` and retry |
| `Connection refused` | sshd not listening; revisit step 18 |
| `Connection timed out` | firewall or network; revisit step 19 |
| `uname: command not found` (Windows) | sshd's default shell isn't Git Bash with `-lc`; revisit step 18.b |

## Step 22 - Register the project

Once the test passes:

```bash
docker compose exec gateway fitt project add <name> \
    --ssh-host <user>@<satellite-hostname> \
    --path <absolute-path-to-repo-on-satellite> \
    --test-command "<how to run tests, e.g. 'pytest -q'>"
```

Path format: POSIX on Linux/macOS/WSL (`/home/you/src/my-project`);
Git-Bash-style on Windows Git Bash (`/c/src/my-project`).

Verify:

```bash
docker compose exec gateway fitt project list
```

## Step 23 - Smoke-test end to end

In Telegram, using a tool-capable alias, send:

> Call spec_list with project set to `<name>` and tell me what comes back.

You should see real output. Check logs if it stalls:

```bash
tail -f "$FITT_HOME/logs/gateway.log" | grep -v telegram
```

Look for a `tool.invoked` event with `ok: true`. That's the marker
that SSH dispatch is working end-to-end.

## Adding another project

Repeat steps 20-22 for each project/satellite pair. Step 17's key
(the gateway's public SSH key) is the same for every satellite —
one key, authorised on N machines. Step 18 also stays put: sshd
is a per-machine thing, not a per-project thing.

---

## Adding a skill

A skill is a directory under `$FITT_HOME/skills/<name>/`
containing a `SKILL.md` file. The frontmatter declares the skill's
`name` and `description`; the body is a recipe the agent reads on
demand when the user's request matches the description.

There's a working sample under `docs/sample-skills/fitt-status/`
in the repo. Drop it in:

```bash
mkdir -p $FITT_HOME/skills
cp -r docs/sample-skills/fitt-status $FITT_HOME/skills/
docker compose restart fitt-gateway
```

Confirm the loader saw it:

```bash
docker compose logs fitt-gateway 2>&1 | grep skills.loaded
# event=skills.loaded skill_name=fitt-status ...
```

In Telegram, send "give me FITT's status" — the agent should
load the recipe and produce a formatted status report. Tool
calls are visible in Telegram (look for "Read", "Ran
list_capabilities", etc.).

### Authoring a SKILL.md

```markdown
---
name: <directory-name>             # required, must match dir basename
description: <one short sentence>  # required, capped at 80 chars in prompt
prerequisites: []                  # optional, list of FITT tool names
---

# <Title>

When <user-request-pattern>, run these tools in order:

1. `<tool_name>` — what it does
2. ...

Format the result as: ...
```

Keep the description tight — the model picks skills based on it,
and the description is the only part shipped in the system prompt
on every request. The body is loaded on demand.

### What edits take effect when

| Edit | Action needed |
|------|---------------|
| New skill (new SKILL.md anywhere) | `docker compose restart fitt-gateway` |
| Renamed skill directory | `docker compose restart fitt-gateway` |
| Frontmatter `description` change | `docker compose restart fitt-gateway` (description is in the system prompt, computed at boot) |
| SKILL.md body change | Start a new session (`/session new <name>`). The body is read on demand by the agent's `read_file` call; existing sessions cache the prior body in their conversation history. |
| `memory.skills_enabled: false` toggle | `docker compose restart fitt-gateway` |

### Trivial skills won't fire

Skills whose body is just a paraphrase of the description (e.g.
"reply in French") won't be loaded — the model already knows how
to answer and skips the recipe call. Skills only fire when the
recipe contains genuinely non-obvious info (specific tool calls,
specific output formats, environment-specific knowledge).

When writing a skill, ask: "what does this recipe tell the model
that it can't already do?" If the answer is "nothing meaningful,"
the recipe won't be used.

---

## Web search

The agent has a `web_search` tool wired up out of the box. Ask
questions like "what's the latest version of Python?" or "what
happened with `<recent thing>`?" and the agent will call
`web_search` and answer from the results. The default backend
is DuckDuckGo via the [`ddgs`](https://pypi.org/project/ddgs/)
PyPI package — no API key, nothing to configure for the common
case. From Telegram, just send the question; tool calls are
visible inline in the bot's reply (look for `Ran web_search`).

Operator note: the backend is selected in `config.yaml`'s
`web.search_backend` field (default `ddgs`). The tool name
`web_search` and its JSON schema are stable across backends, so
switching to a future provider (SearXNG on the hub, Brave-free,
Exa) is a single config flip plus a `docker compose restart
fitt-gateway`. Provider implementations live under
`gateway/src/gateway/tools/web_providers/`; adding a new one is
a single Python file. See the gateway README's "Web search"
subsection for the architecture.

---

## Resilience checks (5 min)

On the Hub:

```bash
# Reboot test
sudo reboot           # or your platform's equivalent
# After reboot, from anywhere on Tailscale:
curl http://<hub-tailscale-ip>:8421/health      # should respond within 60s

# Crash test - kill the gateway container, watch Docker restart it
docker kill fitt-gateway
sleep 10
curl http://<hub-tailscale-ip>:8421/health      # should respond again
```

The compose file's `restart: unless-stopped` policy means Docker
restarts any container that crashes. Host reboot + Docker auto-
start (enabled by default on Container Station / Docker Desktop)
means the hub comes back by itself.

---

## You're done

What you have now:

- Three containers on the Hub (gateway, telegram-bot, open-webui)
  behind one `docker compose` command, routing chat requests
  between local satellites and the cloud.
- IDE chat on any laptop that can reach the Hub over Tailscale.
- One Bearer token, one config, swappable models.
- All persistent state under `$FITT_HOME` - back that folder up,
  you have the whole hub.
- Cost visibility. From the repo root:
  `docker compose exec gateway fitt cost` summarises MTD spend.

Next: live with it for a week or two before starting the next
phase - see [`../FITT_ROADMAP.md`](../FITT_ROADMAP.md) guiding
principle 9.

---

## Updating the hub

```bash
cd /path/to/home-ai-cluster
git pull
docker compose build         # rebuild changed images
docker compose up -d         # recreate changed containers
```

Compose only recreates services whose image changed, so this is
safe to run any time. Containers that didn't change keep running.

---

## Troubleshooting

`gateway/README.md` has a dedicated troubleshooting section: auth
401, `/ready` 503, streaming cost=0, firewall issues, and the
first-response `smoke-compose.sh` script.

### What happens on an upstream timeout

If a chat turn shows a ⏱️ or ⚠️ message in Telegram, here's
what each means and where to look.

| User sees | Meaning | Where to look |
|---|---|---|
| `⏱️ Upstream <alias> went silent after Ns — likely queued. Try again, or pick a different alias.` | The gateway's upstream LLM took longer than `upstream_timeout_secs` (default 300s). Common with NVIDIA Build's free tier under load. | `gateway.log`, grep for the request_id printed in the message: rows with `status=upstream_silent` |
| `⚠️ FITT gateway unreachable: ConnectError` | Bot couldn't connect to the gateway at all. Gateway is down, network partition, Tailscale flap. | `docker compose ps`, `docker compose logs --tail=100 fitt-gateway` |
| `⏱️ FITT didn't respond in time on the bot side. Configuration drift…` | The bot's read-timeout fired before the gateway returned. With Phase 4.9 defaults this should never happen — it means the bot's read-timeout is now ≤ the gateway's `upstream_timeout_secs`. Fix by raising the bot's value or lowering the gateway's. | Both timeouts in `~/.fitt/config.yaml` (gateway side) and the bot's `_STREAM_TIMEOUT_S` (compile-time) |
| `⚠️ Upstream stopped responding mid-reply` | Stream started, then died mid-flight. Provider crashed or the connection dropped. | `gateway.log` for `status=stream_failure` |
| `⏳ Rate limited, retry in Ns` | Upstream returned 429 / 503 with Retry-After. Wait the indicated time. | — |

The `(req: <8chars>)` tag at the end of every ⚠️ message is
the short request_id. Paste it as a single grep across both
log files to see the entire turn end-to-end:

```bash
grep abc12345 ~/.fitt/logs/telegram-bot.log
grep abc12345 ~/.fitt/logs/gateway.log
```

Adjusting timeouts. Gateway's `upstream_timeout_secs` is in
`~/.fitt/config.yaml` (top-level field, default 300s). The
bot's read-timeout is `_STREAM_TIMEOUT_S` in
`telegram-bot/src/fitt_telegram_bot/gateway_client.py` (default
360s). The invariant is: **bot read-timeout > gateway
`upstream_timeout_secs`**, with enough margin (~60s) for the
gateway to serialize its error response. Raise both together
if you have legitimately slow upstreams; leave them at default
otherwise.

## Common slip-ups

- **`OLLAMA_HOST=0.0.0.0`** not taking effect on the satellite:
  you changed the env var but didn't restart Ollama.
- **Firewall rule on Public profile**: Windows marked your
  Tailscale interface as Public. Settings -> Network -> Tailscale
  -> Private.
- **Trailing whitespace in the Bearer token**: copy-paste ate a
  space. Regenerate with the one-liner in 5.3.
- **Wrong LAN IP in `config.yaml`**: your satellite got a new IP
  after sleep/wake. Use a DHCP reservation (step 11) or an mDNS
  hostname.
- **`secrets.yaml` world-readable on first boot**: the gateway
  refuses to load. `chmod 0600` the file and
  `docker compose restart gateway`.
- **`$FITT_HOME` on host owned by a different uid than
  `PUID`/`PGID` in `.env`**: the gateway can't write session
  history or logs. Either `chown -R $PUID:$PGID $FITT_HOME` or
  set `PUID`/`PGID` to match the host owner.
- **Containers keep restarting on a QNAP**: check the container
  logs in Container Station. The most common cause is a
  `depends_on: condition: service_healthy` rejection on older
  QTS versions - drop the `condition:` per Step 6.A's note and
  recreate the application.
- **QNAP: hub can't reach tailnet IPs (`curl http://100.x.y.z/...`
  from the NAS shell times out, but the same URL works from a
  laptop on the tailnet).** Tailscale installed as a Docker
  container doesn't always install a host-side route to `100.x/10`.
  The Tailscale container can *expose* the NAS to the tailnet
  (inbound traffic works, Jellyfin/Plex etc. are reachable
  remotely), but the NAS host and its other bridge-networked
  containers have no route *out*. Two fixes:
  - Use LAN IPs for hub->satellite traffic (step 11). Works out
    of the box because home-LAN routing is already in place.
  - Install Tailscale as a QNAP QPKG instead of a Docker
    container (App Center -> Tailscale). Creates a real
    `tailscale0` interface on the host, adds the tailnet route,
    and bridge-networked containers inherit it automatically.
- **Gateway can't resolve Tailscale hostnames (`*.ts.net` or
  MagicDNS short names) from inside the container, even after
  installing the Tailscale QPKG on the NAS.** Docker bridge
  containers get their DNS from Docker's embedded resolver,
  which forwards to whatever's in `/etc/resolv.conf`. Tailscale's
  MagicDNS lives on `100.100.100.100`, which the container's
  upstream DNS doesn't know about.
  docker-compose.yml now sets `dns: [100.100.100.100, 1.1.1.1]`
  on the gateway service so bridge containers resolve tailnet
  names directly via MagicDNS. If you're still on an older
  compose file, add that two-line block under the gateway
  service and recreate. Using raw `100.x.y.z` Tailscale IPs in
  config.yaml also works and skips DNS entirely.
- **Container Station GUI didn't read `.env`** when importing the
  compose file. Expected — the GUI's compose importer doesn't
  support `.env` substitution. Use 6.B (`docker compose up -d`
  over SSH) or hardcode the env values into the pasted YAML.
- **`git: command not found` in QNAP SSH**: QNAP's minimal SSH
  shell doesn't ship git. Clone on a laptop and copy the repo
  over, or install Entware + git on the NAS. See step 3.2.
- **Permission denied copying configs into `$FITT_HOME`**:
  non-admin QNAP users often can't write under `/share/Public/`.
  SSH as admin for the initial setup. See step 5.1.
- **Port 8080 already in use on QNAP**: the NAS admin UI claims
  it. The compose file now defaults the gateway to port 8421 to
  dodge this; if 8421 is also taken on your hub, set `FITT_PORT`
  and `GATEWAY_HOST_PORT` in `.env` to any free port.
