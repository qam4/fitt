#requires -Version 5.1

<#
.SYNOPSIS
    Uninstall the FITT Telegram bot Windows service.

.DESCRIPTION
    Stops and removes the service (default name: FITTTelegramBot).
    Leaves ~/.fitt/telegram/ alone so per-chat prefs survive.

    Run from an elevated PowerShell prompt.
#>

[CmdletBinding()]
param([string]$ServiceName = 'FITTTelegramBot')

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

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    Write-Host "==> Stopping and removing '$ServiceName'." -ForegroundColor Cyan
    $nssm = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($nssm) {
        & nssm.exe stop $ServiceName confirm | Out-Null
        Start-Sleep -Seconds 2
        & nssm.exe remove $ServiceName confirm | Out-Null
    } else {
        sc.exe stop $ServiceName | Out-Null
        sc.exe delete $ServiceName | Out-Null
    }
} else {
    Write-Host "Service '$ServiceName' is not installed." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Uninstall complete." -ForegroundColor Green
Write-Host "Note: ~/.fitt/telegram/prefs.json is untouched. Remove it manually if you want to reset per-chat preferences."
