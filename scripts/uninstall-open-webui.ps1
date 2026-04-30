#requires -Version 5.1

<#
.SYNOPSIS
    Tear down Open WebUI and clean up its firewall rule.

.DESCRIPTION
    Runs `docker compose down -v` to stop the container and remove
    its named volumes, then deletes the firewall rule.

    Leaves the ./open-webui-data bind-mount directory intact - it
    contains your chat history and admin account, which you may
    want to keep if reinstalling.

    Run from an elevated PowerShell prompt.
#>

[CmdletBinding()]
param([string]$WorkingDir)

$ErrorActionPreference = 'Stop'

function Assert-Elevated {
    $principal = New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent()
    )
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "This script must be run from an elevated PowerShell prompt."
    }
}

Assert-Elevated

$dir = if ($WorkingDir) { $WorkingDir } else { Split-Path -Parent $PSScriptRoot }
Push-Location $dir
try {
    & docker compose down -v 2>&1 | Out-Null
} finally {
    Pop-Location
}

Remove-NetFirewallRule -DisplayName 'FITT Open WebUI (Private only)' -ErrorAction SilentlyContinue

Write-Host "Open WebUI uninstalled. ./open-webui-data is preserved."
