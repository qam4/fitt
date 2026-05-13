# Pointing a Coding Agent at FITT (Router Mode)

How to configure a coding-agent tool (Aider, opencode, Claude
Code, Cursor, Codex, Kiro CLI, ...) to use FITT as its
OpenAI-compatible model backend, with FITT acting as a thin
alias-routing proxy instead of its usual agent layer.

For the motivation and mechanics of router mode see the
[Aider collision entry in docs/observed-issues.md](./observed-issues.md#fitt-capability-block-leaks-into-coding-agent-clients-aider)
and the `coding-agent` section of
[gateway/src/gateway/auth.py](../gateway/src/gateway/auth.py).

## The two-line contract

A coding-agent tool gets router mode from FITT when the gateway
sees either of these on the request:

1. `X-FITT-Client: coding-agent` header, or
2. A bearer token whose `client:` tag in `secrets.yaml` is
   `coding-agent`.

Either is sufficient. If both are set they have to agree; the
auth middleware rejects mismatches with a 400 so silent
misconfig doesn't happen.

**Preferred approach:** tag the token. Keeps the client
config free of FITT-specific headers, makes per-client audit
entries clean, and means swapping a CLI between "just use
FITT" and "fully agent-layered" is a token change, not a
config edit.

## Setup — shared prerequisites

On the Hub (FITT gateway host), one-time:

1. **Generate a fresh token** for the CLI. One token per tool
   makes the audit log readable — you can tell which CLI
   produced which call.

   ```bash
   # On any machine; just needs to be unguessable.
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

2. **Add it to `secrets.yaml`** with `client: coding-agent`:

   ```yaml
   allowed_tokens:
     - name: opencode        # or aider, claude-code, codex, etc.
       token: <the-token-you-just-generated>
       client: coding-agent
   ```

3. **Restart the gateway** (or wait for the next natural
   restart — `secrets.yaml` isn't hot-reloaded today).

4. **Watch the boot log for `alias_probe.ok`** per alias. If
   any alias probes as `alias_probe.narrated`, swap that
   alias before pointing a coding-agent at FITT. You don't want
   to find out mid-editing-session that the model emits
   prose instead of tool_calls.

On the machine running the CLI:

- Make sure FITT's HTTP endpoint is reachable. On Tailscale:
  `http://<hub-tailnet-name-or-ip>:8080`. Confirm with
  `curl http://hub:8080/health`.
- Export the token:

  ```bash
  export FITT_TOKEN=<the-token>
  ```

## Per-tool recipes

### opencode

opencode uses OpenAI-compatible providers via AI SDK and
supports custom `options.headers`.

`~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "fitt": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "FITT Gateway",
      "options": {
        "baseURL": "http://hub:8080/v1",
        "apiKey": "{env:FITT_TOKEN}"
      },
      "models": {
        "fitt-smart":   { "name": "FITT smart" },
        "fitt-default": { "name": "FITT default" },
        "fitt-fast":    { "name": "FITT fast" }
      }
    }
  }
}
```

Notes:

- **`baseURL` ends in `/v1`.** FITT's chat endpoint is
  `POST /v1/chat/completions`; opencode appends the path
  itself.
- **Token tagging is enough.** The `allowed_tokens` entry
  above sets `client: coding-agent`, so no
  `X-FITT-Client` header is needed in opencode's config.
  If you'd rather set the header explicitly (leaving the
  token untagged), add:

  ```json
  "headers": { "X-FITT-Client": "coding-agent" }
  ```

  to `options` and drop the `client:` tag in `secrets.yaml`.
  Both work; one source of truth is easier to reason about.
- Use `/models` inside opencode to pick a FITT alias.

### Aider

Aider reads OpenAI-compatible models via `--openai-api-base`
and standard headers. `~/.aider.conf.yml` or `.aider.conf.yml`
in the project root:

```yaml
openai-api-base: http://hub:8080/v1
openai-api-key: ${FITT_TOKEN}
model: fitt-smart
```

Aider sends `Authorization: Bearer $FITT_TOKEN` and doesn't
need the header because the token is tagged. If you prefer
the header approach:

```yaml
extra-headers:
  X-FITT-Client: coding-agent
```

### Claude Code, Codex, Cursor agent mode, Kiro CLI

Pattern is the same as Aider / opencode — point the tool's
OpenAI-compatible endpoint at `http://hub:8080/v1`, use the
tagged token as the API key. Specific config file varies by
tool; check each tool's "custom model provider" docs.

## Verification

You're in router mode when:

1. The CLI's first turn doesn't mention FITT's tools. Aider
   doesn't ask about `read_file` / `write_file` / etc.;
   opencode doesn't enumerate `list_capabilities`. The CLI's
   own agent owns the conversation.
2. `fitt inbox` on the Hub shows audit entries for the model
   calls but no `tool_executed` events from FITT tools (the
   CLI's own tool executions happen locally, not through
   FITT).
3. If you explicitly ask "what tools do you have?", the CLI
   answers with its own tool set (file edits, shell, diffs,
   etc.) — not FITT's.

You're NOT in router mode if:

- The CLI's first turn acknowledges FITT tools or tries to
  call `list_capabilities`.
- You see a capability block in the dispatched request body
  (check with `server.log_bodies: true` in config.yaml).
- `fitt inbox` shows `tool_executed` events that weren't
  triggered by you directly on the Hub.

Any of those mean the client tag isn't landing. Usual
culprits: token without a `client:` tag AND no header;
header typo (`X-FITT-Client`, not `X-Fitt-Client` — case
doesn't matter but the spelling does); gateway not restarted
after the `secrets.yaml` edit.

## What router mode does NOT give up

FITT still:

- Resolves aliases (`fitt-smart` → whichever model is
  currently bound).
- Dispatches via LiteLLM with the configured backend +
  fallback chain.
- Tracks cost and logs `X-FITT-Backend` on the response.
- Writes an audit-log entry per model call.
- Runs the boot-time alias probe at gateway startup.

All the no-subscription alias routing you use FITT for
continues to work. What router mode skips is the FITT agent
layer (tools, memory, capability block, approval middleware)
that would duplicate or conflict with the coding agent's own
agent loop.

## When NOT to use router mode

If you want FITT's tools (file / git / shell / cron / etc.)
exposed to the coding session, don't flip router mode.
Examples:

- Telegram conversations. Always agent mode (the bot
  explicitly tags `telegram`).
- Continue in VS Code Chat mode (vs. Agent mode). Chat mode
  wants FITT's tool list; tag the token `ide` instead of
  `coding-agent`.
- Ad-hoc curl / Open WebUI exploration. Default (untagged)
  stays `webui`, which is agent mode.

Router mode is specifically for clients where the client
itself is a coding agent and FITT should be a transparent
pipe. If you're not sure, default to agent mode — silent
feature-stripping is worse than an extra capability block.
