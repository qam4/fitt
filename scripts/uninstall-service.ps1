#requires -Version 5.1

<#
.SYNOPSIS
    Uninstall the FITT Gateway Windows service.

.DESCRIPTION
    Stops and removes the Windows service (default name: FITTGateway)
    via NSSM, and removes the firewall rule created by install-service.ps1.

    Run from an elevated PowerShell prompt.

.PARAMETER ServiceName
    Name of the Windows service. Default: "FITTGateway".

.PARAMETER KeepLogs
    If set, service-log files under ~/.fitt/logs are preserved. The
    gateway's own structured log is always preserved (it lives in the
    same dir but is managed by the app, not this script).
#>

[CmdletBinding()]
param(
    [string]$ServiceName = 'FITTGateway',
    [switch]$KeepLogs
)

$ErrorActionPreference = 'Stop'

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Assert-Elevated {
    $principal = New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent()
    )
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "This script must be run from an elevated PowerShell prompt."
    }
}

Assert-Elevated

$nssm = Get-Command nssm.exe -ErrorAction SilentlyContinue
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue

if ($svc) {
    if ($nssm) {
        Write-Step "Stopping and removing service '$ServiceName'."
        & nssm.exe stop $ServiceName confirm | Out-Null
        Start-Sleep -Seconds 2
        & nssm.exe remove $ServiceName confirm | Out-Null
    } else {
        Write-Warning "NSSM not on PATH. Falling back to sc.exe delete."
        sc.exe stop $ServiceName | Out-Null
        sc.exe delete $ServiceName | Out-Null
    }
} else {
    Write-Host "Service '$ServiceName' is not installed - nothing to remove." -ForegroundColor Yellow
}

$ruleName = 'FITT Gateway (Private only)'
$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Step "Removing firewall rule '$ruleName'."
    Remove-NetFirewallRule -DisplayName $ruleName
} else {
    Write-Host "Firewall rule '$ruleName' not present." -ForegroundColor Yellow
}

if (-not $KeepLogs) {
    $serviceLogs = @(
        Join-Path $env:USERPROFILE '.fitt\logs\service.stdout.log'
        Join-Path $env:USERPROFILE '.fitt\logs\service.stderr.log'
    )
    foreach ($p in $serviceLogs) {
        if (Test-Path $p) {
            Write-Step "Removing $p"
            Remove-Item -Force $p
        }
    }
}

Write-Host ""
Write-Host "Uninstall complete." -ForegroundColor Green
Write-Host "Note: your ~/.fitt config, secrets, and gateway.log are untouched."
