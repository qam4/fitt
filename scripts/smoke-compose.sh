#!/usr/bin/env bash
# Smoke test for the Docker hub. Brings up the gateway with a
# minimal temp config, waits for /health and /v1/models, tears down.
#
# Usage:
#   scripts/smoke-compose.sh
#
# Exits 0 on success, non-zero with a message on any failure.
# Run from the repo root so compose can find docker-compose.yml.
#
# What it does NOT test:
#   - Telegram bot (needs a real bot token; use your live bot for
#     that, or stub the telegram API).
#   - Open WebUI (needs a browser click-through).
#   - Actual LLM calls (no upstream creds in a smoke run).
#
# So this validates "the image builds, the gateway boots, routes
# up to /health and /v1/models respond as expected." That's
# enough to catch most regressions without real secrets.

set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
    echo "smoke-compose: docker not found on PATH" >&2
    exit 127
fi

workdir="$(mktemp -d)"
trap 'docker compose -f docker-compose.yml --env-file "$workdir/.env" down --remove-orphans 2>/dev/null || true; rm -rf "$workdir"' EXIT

echo "smoke-compose: FITT_HOME=$workdir"

# Minimal config that the gateway will accept without reaching out
# to any real upstreams. A single openrouter model keeps the alias
# graph valid; the test token doesn't have to work for /health or
# /v1/models to succeed.
cat >"$workdir/config.yaml" <<'YAML'
server:
  host: 0.0.0.0
  port: 8080
  log_level: info

aliases:
  fitt-smart: openrouter-sonnet

models:
  - id: openrouter-sonnet
    backend: openrouter
    model: anthropic/claude-sonnet-4.5
    cost_per_mtok_in:  3.00
    cost_per_mtok_out: 15.00

logging:
  dir: /fitt/logs
  retention_days: 7

memory:
  enabled: false
  identity_dir: /fitt/identity
  sessions_dir: /fitt/sessions
YAML

cat >"$workdir/secrets.yaml" <<'YAML'
allowed_tokens:
  - name: smoke
    token: SMOKE_TEST_TOKEN_AAAAAAAAAAAAAAAAAAAAAAAAAAA

openrouter_api_key: sk-or-smoke-not-a-real-key
YAML
chmod 0600 "$workdir/secrets.yaml"

cat >"$workdir/.env" <<YAML
FITT_HOME=$workdir
PUID=$(id -u)
PGID=$(id -g)
TZ=UTC
FITT_BEARER_TOKEN=SMOKE_TEST_TOKEN_AAAAAAAAAAAAAAAAAAAAAAAAAAA
YAML

echo "smoke-compose: building gateway image..."
docker compose --env-file "$workdir/.env" build gateway

echo "smoke-compose: starting gateway..."
docker compose --env-file "$workdir/.env" up -d gateway

# Poll /health for up to 60s.
health_ok=""
for _ in $(seq 1 30); do
    if curl -sf http://localhost:8080/health >/dev/null; then
        health_ok=1
        break
    fi
    sleep 2
done
if [[ -z "$health_ok" ]]; then
    echo "smoke-compose: FAIL /health did not respond within 60s" >&2
    docker compose --env-file "$workdir/.env" logs gateway >&2 || true
    exit 1
fi
echo "smoke-compose: /health OK"

# /v1/models is public (no auth) and must list the single alias.
if ! curl -sf http://localhost:8080/v1/models | grep -q "fitt-smart"; then
    echo "smoke-compose: FAIL /v1/models did not list fitt-smart" >&2
    exit 1
fi
echo "smoke-compose: /v1/models OK"

echo "smoke-compose: all checks passed"
