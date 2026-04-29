# Prerequisites

> What must be installed, configured, and reachable on each machine
> **before** starting the FITT gateway for the first time.

This is the one-page operational checklist. Do these in order; every
step is short. See [`accounts-setup.md`](./accounts-setup.md) for the
external accounts (OpenRouter, Telegram, optional Anthropic) that go
alongside this.

---

## Machines in this setup

| Role    | Machine                    | What runs on it                               |
|---------|----------------------------|-----------------------------------------------|
| Hub     | Always-on desktop          | FITT gateway, smaller Ollama model (fallback) |
| Compute | Laptop with larger GPU     | Larger Ollama model (primary for `fitt-default`) |
| Clients | Phone, laptop IDE, browser | Talk to the gateway over Tailscale            |

The gateway only *needs* the Hub to be up. Compute and Clients are
optional at boot; the gateway degrades gracefully.

---

## 1. Tailscale (both machines + phone)

FITT uses Tailscale as its network perimeter. Nothing is exposed to
the public internet; every device that can talk to the gateway is
already authenticated by Tailscale at the network layer.

**Steps:**

1. Install Tailscale on Hub, Compute, and phone if not already done.
   ([tailscale.com/download](https://tailscale.com/download))
2. Sign in with the same account on all three.
3. Confirm they're all in the same tailnet:

   ```powershell
   tailscale status
   ```

   Every device should have an IP in `100.64.0.0/10`.
4. Note the Tailscale IPs for later:
   - **Hub's Tailscale IP** — the URL your IDE and phone will point
     at.
   - **Compute's Tailscale IP** — goes into `config.yaml` as the
     `endpoint` for the primary `ollama` model.

**Optional but nice:** enable **MagicDNS** in the Tailscale admin
console. Then you can use hostnames (e.g. `http://laptop.tail-scale.ts.net:8080`)
instead of raw IPs.

**ACLs:** Tailscale's default ACL allows every device on your tailnet
to reach every other device. That's what FITT expects. Don't tighten
ACLs unless you know exactly which ports to allow.

---

## 2. Ollama on Compute (the laptop)

1. Install Ollama for Windows:
   [ollama.com/download](https://ollama.com/download). Runs as a
   Windows service; no admin needed after install.

2. **Critical: set `OLLAMA_HOST=0.0.0.0`.** By default Ollama listens
   only on `localhost`, so the Hub can't reach it over Tailscale.

   In Windows Settings → "Edit the system environment variables" →
   Environment Variables, add a new **User variable**:

   - Name: `OLLAMA_HOST`
   - Value: `0.0.0.0`

   Then **quit Ollama from the system tray and restart it** (or log
   out and back in). No restart = no reload of env vars.

3. Verify Ollama is reachable from the Hub:

   ```powershell
   # From the Hub:
   curl http://<compute-tailscale-ip>:11434/api/tags
   ```

   A JSON list (possibly empty) means you're good. A connection
   refused means Ollama didn't pick up `OLLAMA_HOST=0.0.0.0`.

4. Pull the primary model:

   ```powershell
   ollama pull qwen2.5-coder:14b
   ```

   ~9 GB download. Later you can swap this model with a config-only
   change on the Hub.

---

## 3. Ollama on Hub (the desktop)

Same install, same `OLLAMA_HOST=0.0.0.0` setting (lets you query it
from the laptop if you ever want bidirectional routing), then:

```powershell
ollama pull qwen2.5-coder:7b
```

Smaller model, fits the Hub's 8 GB VRAM comfortably. Used as the
fallback when Compute is asleep.

---

## 4. Python 3.11+ on Hub

The gateway is a Python service.

```powershell
python --version
```

Need 3.11 or newer. If missing:
[python.org/downloads](https://www.python.org/downloads/) — install
the Windows 64-bit installer and check "Add Python to PATH."

---

## 5. NSSM on Hub (for Windows service install)

NSSM wraps any `.exe` as a proper Windows service with auto-restart.
FITT's install script uses it.

```powershell
# Chocolatey one-liner:
choco install nssm

# Or grab the binary from https://nssm.cc/download and drop it
# somewhere on PATH.
```

Only needed for the production service install in Phase 1k. You can
skip it if you're just running `python -m gateway` in a dev shell for
now.

---

## 6. Windows Defender Firewall on Hub

Once the gateway is installed, you'll add **one inbound rule**:

- **Program**: `python.exe` (or the gateway binary), OR
- **Port**: TCP 8080
- **Profile**: Private only (Tailscale registers as a Private network)
- **Public**: blocked

`scripts/install-service.ps1` (Phase 1k) creates this rule. You can
also create it manually via `wf.msc`.

**Verify after**: from a device *outside* your tailnet (phone on
mobile data is easiest):

```
nmap -p 8080 <hub-public-ip>
```

Should show **closed** or **filtered**. If open, the rule isn't
restricted to Private profile.

---

## Checklist before starting the gateway

On the Hub:
- [ ] Tailscale up, status shows Compute + phone.
- [ ] Python 3.11+.
- [ ] Ollama installed, `OLLAMA_HOST=0.0.0.0` set, `qwen2.5-coder:7b`
      pulled.
- [ ] NSSM on PATH (if installing as a service).
- [ ] Cloned this repo and run `pip install -e ".[dev]"` inside
      `gateway/`.

On Compute (laptop):
- [ ] Tailscale up.
- [ ] Ollama installed, `OLLAMA_HOST=0.0.0.0` set, `qwen2.5-coder:14b`
      pulled.
- [ ] Verified from Hub: `curl http://<compute-tailscale-ip>:11434/api/tags`
      returns JSON.

Accounts ([`accounts-setup.md`](./accounts-setup.md)):
- [ ] OpenRouter API key in `~/.fitt/secrets.yaml`.
- [ ] Bearer token generated and in `~/.fitt/secrets.yaml`.
- [ ] (Optional) Telegram bot token + user ID if you've created the
      bot already.

Then follow the install steps in [`gateway/README.md`](../gateway/README.md).
