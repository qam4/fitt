#requires -Version 5.1

<#
.SYNOPSIS
    Install the FITT Telegram bot as a Windows service.

.DESCRIPTION
    Registers the telegram-bot Python package as a Windows service
    named "FITTTelegramBot" using NSSM. Auto-starts at boot,
    restarts 30 seconds after any failure.

    Requires the gateway to be installed first: the bot reads the
    same ~/.fitt/config.yaml and ~/.fitt/secrets.yaml and imports
    the gateway package for its session registry.

    Run from an elevated PowerShell prompt.

.PARAMETER ServiceName
    Name of the Windows service. Default: "FITTTelegramBot".

.PARAMETER SetupVenv
    When set, runs `uv sync` in telegram-bot/ before registering
    the service. Requires uv on PATH.

.PARAMETER PythonExe
    Escape hatch for users who manage their own Python environment.
    Must point at an interpreter where `pip install -e .` has been
    run against the telegram-bot/ package.

.PARAMETER WorkingDir
    Path to the repo root. Default: the parent of this script's
    directory.

.EXAMPLE
    .\install-telegram-bot.ps1 -SetupVenv
#>

[CmdletBinding()]
param(
    [string]$ServiceName = 'FITTTelegramBot',
    [switch]$SetupVenv,
    [string]$PythonExe,
    [string]$WorkingDir
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

function Resolve-DefaultWorkingDir {
    Split-Path -Parent $PSScriptRoot
}

function Get-VenvPython {
    param([string]$RepoRoot)
    Join-Path $RepoRoot 'telegram-bot\.venv\Scripts\python.exe'
}

function Invoke-UvSync {
    param([string]$RepoRoot)

    $uv = Get-Command uv.exe -ErrorAction SilentlyContinue
    if (-not $uv) {
        throw @"
-SetupVenv requires uv on PATH. Install uv with:
    winget install --id=astral-sh.uv -e
Then re-run this script.
"@
    }
    Write-Step "Running 'uv sync' in telegram-bot\"
    Push-Location (Join-Path $RepoRoot 'telegram-bot')
    try {
        & uv.exe sync
        if ($LASTEXITCODE -ne 0) {
            throw "uv sync failed with exit code $LASTEXITCODE. See output above."
        }
    } finally {
        Pop-Location
    }
}

function Resolve-Python {
    param([string]$RepoRoot, [string]$ExplicitPythonExe, [bool]$DoSetupVenv)

    if ($ExplicitPythonExe) {
        if (-not (Test-Path $ExplicitPythonExe)) {
            throw "Python interpreter not found at: $ExplicitPythonExe"
        }
        return $ExplicitPythonExe
    }

    if ($DoSetupVenv) {
        Invoke-UvSync -RepoRoot $RepoRoot
    }

    $venvPython = Get-VenvPython -RepoRoot $RepoRoot
    if (-not (Test-Path $venvPython)) {
        throw @"
Expected Python at:
    $venvPython

Run 'uv sync' in telegram-bot\ first, or re-run this script with
-SetupVenv to do it automatically.
"@
    }
    return $venvPython
}

function Assert-NssmPresent {
    $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw @"
NSSM is required but not on PATH.
    winget install --id=NSSM.NSSM -e
"@
    }
    return $cmd.Source
}

function Assert-SecretsHaveTelegram {
    $secretsPath = Join-Path $env:USERPROFILE '.fitt\secrets.yaml'
    if (-not (Test-Path $secretsPath)) {
        throw "~\.fitt\secrets.yaml missing. Install the gateway first; see docs\quickstart.md."
    }
    # Light-touch check: bot_token line is present and not the
    # placeholder. We don't parse YAML here to avoid deps.
    $content = Get-Content $secretsPath -Raw
    if ($content -notmatch 'bot_token') {
        throw "secrets.yaml does not contain a telegram.bot_token entry. Fill it in before installing the bot."
    }
    if ($content -match '123456:ABC-REPLACE') {
        throw "secrets.yaml still has the placeholder bot_token. Put your real @BotFather token in first."
    }
}

function Assert-BotImportable {
    param([string]$Python)
    & $Python -c "import fitt_telegram_bot; print(fitt_telegram_bot.__version__)" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw @"
The Python interpreter at:
    $Python
cannot import the 'fitt_telegram_bot' package. Run 'uv sync' in
telegram-bot\ or use -SetupVenv.
"@
    }
}

function Stop-ExistingService {
    param([string]$Name)
    $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($svc) {
        Write-Step "Service '$Name' already exists; stopping and removing."
        & nssm.exe stop $Name confirm | Out-Null
        Start-Sleep -Seconds 2
        & nssm.exe remove $Name confirm | Out-Null
    }
}

function Install-BotService {
    param([string]$Name, [string]$Nssm, [string]$Python, [string]$RepoRoot)

    $fittHome = Join-Path $env:USERPROFILE '.fitt'
    $logDir = Join-Path $fittHome 'logs'
    New-Item -ItemType Directory -Force $logDir | Out-Null

    Write-Step "Registering service '$Name'."
    & $Nssm install $Name $Python '-m' 'fitt_telegram_bot' | Out-Null
    & $Nssm set $Name AppDirectory (Join-Path $RepoRoot 'telegram-bot') | Out-Null
    & $Nssm set $Name DisplayName 'FITT Telegram Bot' | Out-Null
    & $Nssm set $Name Description 'FITT - Telegram bot that forwards to the gateway.' | Out-Null
    & $Nssm set $Name Start SERVICE_AUTO_START | Out-Null

    & $Nssm set $Name AppExit Default Restart | Out-Null
    & $Nssm set $Name AppRestartDelay 30000 | Out-Null
    & $Nssm set $Name AppThrottle 5000 | Out-Null

    & $Nssm set $Name AppStdout (Join-Path $logDir 'telegram-bot.stdout.log') | Out-Null
    & $Nssm set $Name AppStderr (Join-Path $logDir 'telegram-bot.stderr.log') | Out-Null
    & $Nssm set $Name AppStdoutCreationDisposition 4 | Out-Null
    & $Nssm set $Name AppStderrCreationDisposition 4 | Out-Null
    & $Nssm set $Name AppRotateFiles 1 | Out-Null
    & $Nssm set $Name AppRotateOnline 1 | Out-Null
    & $Nssm set $Name AppRotateBytes 10485760 | Out-Null

    $envBlock = "FITT_HOME=$fittHome`0" + "PYTHONUNBUFFERED=1`0"
    & $Nssm set $Name AppEnvironmentExtra $envBlock | Out-Null

    Write-Step "Starting service."
    & $Nssm start $Name | Out-Null
    Start-Sleep -Seconds 3
}

# ---------------------------------------------------------------- main

Assert-Elevated

if (-not $WorkingDir) { $WorkingDir = Resolve-DefaultWorkingDir }
$python = Resolve-Python -RepoRoot $WorkingDir `
                        -ExplicitPythonExe $PythonExe `
                        -DoSetupVenv:$SetupVenv
$nssm = Assert-NssmPresent
Assert-SecretsHaveTelegram
Assert-BotImportable -Python $python

Write-Step "Installing FITT Telegram bot service."
Write-Host "  ServiceName:  $ServiceName"
Write-Host "  PythonExe:    $python"
Write-Host "  WorkingDir:   $WorkingDir"
Write-Host "  NSSM:         $nssm"

Stop-ExistingService -Name $ServiceName
Install-BotService -Name $ServiceName -Nssm $nssm -Python $python -RepoRoot $WorkingDir

Write-Host ""
Write-Host "Service '$ServiceName' installed and started." -ForegroundColor Green
Write-Host "Verify with:  Get-Service $ServiceName"
Write-Host "Logs:         $($env:USERPROFILE)\.fitt\logs\telegram-bot.*.log"
Write-Host ""
Write-Host "Message your bot with /start from an allowlisted Telegram account to confirm end-to-end."
