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

Git is pre-installed on macOS, most Linux distros, and QNAP.
If you need it on Windows:

```powershell
winget install --id=Git.Git -e
```

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

### 5.2 Edit `$FITT_HOME/config.yaml`

Replace the `100.x.y.z` placeholder for `qwen-coder-big` with the
Tailscale IP of the satellite you'll set up in Part B. If you
don't have a satellite yet, leave the placeholder - the gateway
will start, the cloud alias will work, and `/ready` will return
503 until you wire the satellite up.

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

1. Container Station -> **Applications** -> **Create**.
2. Give the application a name (`fitt` is fine).
3. Paste the contents of `docker-compose.yml` from the repo into
   the YAML editor, or point at the file on the share.
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

If something looks wrong, run the smoke script from the repo
root:

```bash
scripts/smoke-compose.sh
```

It builds the gateway image, starts it against a throwaway
config, hits `/health` and `/v1/models`, and tears down. If that
passes, the image is fine and the issue is in your
`$FITT_HOME/config.yaml`, `secrets.yaml`, or `.env`. If it fails,
the build or the entrypoint is broken - read the compose logs
for the real error.

Also useful:

```bash
docker compose ps                   # container states
docker compose logs gateway         # gateway stdout/stderr
docker compose logs telegram-bot    # bot stdout/stderr
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

   Without this, the Hub can't reach Ollama over the tailnet;
   Ollama defaults to listening on loopback only.
2. Restart Ollama so it picks up the env var (quit from the tray
   on Windows/macOS, or `systemctl restart ollama` on Linux).

Verify from the **Hub**:

```bash
curl http://<satellite-tailscale-ip>:11434/api/tags
```

JSON response = good. Connection refused = the env var didn't
take (most common cause: didn't restart Ollama after setting it).

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
