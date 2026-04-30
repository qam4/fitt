# FITT Telegram Bot

Thin Telegram client that forwards messages to the FITT gateway.
Lives on the Hub, runs as a Windows service, uses
`python-telegram-bot` in polling mode.

## Install

See [`../docs/quickstart.md`](../docs/quickstart.md) - the bot install
is steps 8 and onward. Quick version from an elevated shell at the
repo root:

```powershell
.\scripts\install-telegram-bot.ps1 -SetupVenv
```

## Development

```powershell
cd telegram-bot
uv sync
uv run pytest
uv run fitt-telegram-bot
```

## Commands

Once the bot is running and you've messaged `/start`:

| Command                         | Effect                                           |
|---------------------------------|--------------------------------------------------|
| `/start`                        | Greet + show current prefs.                      |
| `/help`                         | List commands.                                   |
| `/session`                      | List sessions; mark current one.                 |
| `/session <id>`                 | Switch this chat's active session.               |
| `/session new <id> [<name>]`    | Create a new session (calls the registry).       |
| `/model`                        | List aliases; mark current.                      |
| `/model <alias>`                | Set this chat's alias (`fitt-default`, `fitt-smart`, ...). |

## Files

- `~/.fitt/telegram/prefs.json` - per-chat alias + session (atomic
  writes).
- Bot token, allowlist, gateway URL, Bearer token all come from
  `~/.fitt/secrets.yaml` (Telegram block) and `~/.fitt/config.yaml`.

## How it decides what to do

1. `update` arrives from Telegram.
2. If `effective_user.id` isn't in `telegram.allowlist_user_ids`,
   silently drop + log.
3. Otherwise route by message type:
   - text -> chat with gateway, stream reply by editing a placeholder message
   - photo -> multimodal chat with gateway
   - voice -> stub "not yet" reply
   - commands (`/start`, `/session`, `/model`, ...) - handled locally
     or via the shared `SessionRegistry`

No fallback providers, no memory of its own - the gateway is the
source of truth.
