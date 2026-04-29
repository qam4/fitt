# Accounts Setup

> Everything you need to create and collect before running FITT.
> One page, copy-pasteable, do it once.

FITT runs on your hardware but talks to a few external services. This
doc enumerates them and shows exactly what to do for each.

## Summary

| Service | Required? | Free tier? | Used from |
|---|---|---|---|
| [Tailscale](#tailscale) | yes | yes | Phase 1 |
| [OpenRouter](#openrouter) | yes (cloud tier) | yes | Phase 1 |
| [GitHub](#github) | yes | yes | Phase 1 |
| [Anthropic (direct)](#anthropic-direct-optional) | no | no | Phase 1 (optional) |
| [Telegram bot](#telegram-bot) | no in Phase 1; yes in Phase 3 | yes | Phase 3 |

Everything you collect goes into `~/.fitt/secrets.yaml` on the desktop.

---

## Tailscale

Already set up per the roadmap. Make sure:

- Tailscale is running on both rigs (desktop + laptop) and on your
  phone.
- You know the desktop's Tailscale IP (e.g. `100.x.y.z`) — you'll
  reference it from the laptop's Continue config.
- You know the laptop's Tailscale IP — you'll reference it from
  `~/.fitt/config.yaml` on the desktop (it's the endpoint for
  `qwen-coder-big`).

To see them:

```powershell
tailscale status
```

Or enable MagicDNS and use hostnames instead of IPs.

## OpenRouter

FITT's primary cloud backend. One API key, many models, free-tier
available.

**Steps:**

1. Go to https://openrouter.ai and sign in with GitHub or Google.
2. Go to https://openrouter.ai/keys and click **Create Key**. Give it
   a name like `fitt-gateway`. No scopes to configure.
3. Copy the key (starts with `sk-or-v1-...`). You won't see it again.
4. Paste into `~/.fitt/secrets.yaml`:
   ```yaml
   openrouter_api_key: sk-or-v1-xxxxxxxxxxxxx
   ```

**Optional (recommended):** add $10 in credits at
https://openrouter.ai/credits. This unlocks higher free-tier rate
limits (1,000 requests/day per free model instead of 50/day) and lets
you use paid models if you ever want them.

**Free models to know about:**

- `qwen/qwen-2.5-coder-32b-instruct:free` — strong local-class coder.
- `deepseek/deepseek-v3-0324:free` — general reasoning.
- `meta-llama/llama-3.3-70b-instruct:free` — general chat.
- Free model availability shifts; see https://openrouter.ai/models?supported_parameters=free

## GitHub

You already have `qam4`. Make sure:

- Private repo `home-ai-cluster` exists (already done).
- `gh auth status` shows you logged in as `qam4` on machines where you
  push. (Already done on the work machine.)
- You have SSH or PAT auth set up on the desktop for `git push`.

## Anthropic (direct) — optional

Skip for Phase 1 unless you know you want direct Claude access (prompt
caching, enterprise features, or OpenRouter's Claude routes stop being
enough).

If you do want it later:

1. Sign up at https://console.anthropic.com.
2. Add billing. Set a monthly spend limit (Usage limits → Monthly
   limit).
3. API Keys → Create Key. Copy it.
4. Uncomment the `anthropic_api_key` line in `~/.fitt/secrets.yaml`
   and paste the key.
5. Uncomment the `claude-sonnet-direct` block in
   `~/.fitt/config.yaml`.
6. Optionally rebind `fitt-smart` to `claude-sonnet-direct` instead of
   `openrouter-sonnet`. Or leave OpenRouter as `fitt-smart` and add a
   new alias like `fitt-claude-direct` for the rare cases you want to
   hit Anthropic directly.
7. Restart the gateway.

## Telegram bot

Not used until Phase 3. You've already created the bot — good.
Populate the secrets file now so you don't touch it twice.

**What you need:**

1. **Bot token** from @BotFather. Looks like `123456:ABC-...`. If you
   no longer have it, send `/mybots` to @BotFather, pick your bot,
   **API Token**, **Show**.
2. **Your numeric user ID.** Message @userinfobot on Telegram, it will
   reply with your ID (a number like `123456789`).

Paste into `~/.fitt/secrets.yaml`:

```yaml
telegram:
  bot_token: 123456:ABC-xxxxxxxxxxxxx
  allowlist_user_ids:
    - 123456789
```

Phase 1 ignores these. Phase 3 picks them up.

## Checklist

Before starting the Phase 1 build on the desktop, you should have:

- [ ] Desktop and laptop on Tailscale, reachable by IP.
- [ ] OpenRouter account + API key.
- [ ] (optional) $10 credit on OpenRouter for higher limits.
- [ ] GitHub auth on the desktop (HTTPS+PAT via `gh` or SSH).
- [ ] Telegram bot token and your user ID (if you already have the
      bot).
- [ ] A random 32+ char string to use as your gateway Bearer token.
      Generate with:
      ```powershell
      python -c "import secrets; print(secrets.token_urlsafe(32))"
      ```

All of these go into `~/.fitt/secrets.yaml` on the desktop. Only the
Bearer token and OpenRouter key are required for Phase 1 to work.
