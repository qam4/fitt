#requires -Version 5.1

<#
.SYNOPSIS
    Install Open WebUI as a Docker compose service pointed at the
    FITT gateway.

.DESCRIPTION
    Reads the Bearer token from ~/.fitt/secrets.yaml, writes it to
    a gitignored .env file at the repo root, runs `docker compose
    up -d open-webui`, and adds a Windows Defender Firewall rule
    for inbound TCP 3000 on the Private profile only (Tailscale).

    Open WebUI serves at http://<hub-tailscale-ip>:3000/. On first
    visit, create the admin account. Signup is disabled after that
    so a second user can't register.

    Requires Docker Desktop running on the Hub.

    Run from an elevated PowerShell prompt.
#>

[CmdletBinding()]
param([string]$WorkingDir)

$ErrorActionPreference = 'Stop'

function Write-Step { param([string]$m) Write-Host "==> $m" -ForegroundColor Cyan }

function Assert-Elevated {
    $principal = New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent()
    )
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "This script must be run from an elevated PowerShell prompt."
    }
}

function Assert-DockerRunning {
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $docker) {
        throw "Docker CLI not on PATH. Install Docker Desktop: winget install --id=Docker.DockerDesktop -e"
    }
    & docker info 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Desktop is installed but not running. Start Docker Desktop and re-run."
    }
}

function Resolve-WorkingDir {
    if ($script:WorkingDir) { return $script:WorkingDir }
    Split-Path -Parent $PSScriptRoot
}

function Read-BearerToken {
    $secretsPath = Join-Path $env:USERPROFILE '.fitt\secrets.yaml'
    if (-not (Test-Path $secretsPath)) {
        throw "~\.fitt\secrets.yaml missing. Install the gateway first."
    }
    # Quick-and-dirty extract of the first allowed_tokens.token.
    # We don't pull in a YAML parser for this.
    $content = Get-Content $secretsPath -Raw
    $match = [regex]::Match(
        $content,
        '(?ms)allowed_tokens:\s*\n\s*-\s*name:[^\n]*\n\s*token:\s*([^\s#]+)'
    )
    if (-not $match.Success) {
        throw "Could not find allowed_tokens[0].token in $secretsPath."
    }
    return $match.Groups[1].Value.Trim()
}

function Write-EnvFile {
    param([string]$Dir, [string]$BearerToken)
    $envFile = Join-Path $Dir '.env'
    Set-Content -Path $envFile -Value "FITT_BEARER_TOKEN=$BearerToken" -Encoding ASCII -NoNewline
    Write-Step "Wrote $envFile"
}

function Ensure-FirewallRule {
    param([string]$Name = 'FITT Open WebUI (Private only)', [int]$Port = 3000)
    Remove-NetFirewallRule -DisplayName $Name -ErrorAction SilentlyContinue
    Write-Step "Adding firewall rule '$Name' - allow TCP $Port inbound, Private profile only."
    New-NetFirewallRule `
        -DisplayName $Name `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $Port `
        -Profile Private `
        -Enabled True | Out-Null
}

# ---------------------------------------------------------------- main

Assert-Elevated
Assert-DockerRunning

$dir = Resolve-WorkingDir
$token = Read-BearerToken
Write-EnvFile -Dir $dir -BearerToken $token

Write-Step "docker compose up -d open-webui"
Push-Location $dir
try {
    & docker compose up -d open-webui
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose up failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

Ensure-FirewallRule

Write-Host ""
Write-Host "Open WebUI is starting." -ForegroundColor Green
Write-Host "Browse to:  http://localhost:3000/          (from the Hub)"
Write-Host "Or:         http://<hub-tailscale-ip>:3000/ (from any Tailscale device)"
Write-Host ""
Write-Host "First visit: create an admin account. Signup is disabled afterwards."
Write-Host "Tear down with: .\scripts\uninstall-open-webui.ps1"
