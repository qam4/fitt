#requires -Version 5.1

<#
.SYNOPSIS
    Install the FITT Gateway as a Windows service.

.DESCRIPTION
    Registers the gateway as a Windows service named "FITTGateway"
    using NSSM. Configures auto-start at boot, 30-second restart on
    failure, and log rotation.

    Also creates a Windows Defender Firewall rule that allows inbound
    TCP 8080 on the Private network profile only (Tailscale registers
    as Private). The public profile remains blocked.

    Run from an elevated PowerShell prompt.

.PARAMETER ServiceName
    Name of the Windows service. Default: "FITTGateway".

.PARAMETER Port
    TCP port the gateway listens on. Default: 8080.

.PARAMETER PythonExe
    Full path to the Python interpreter to run. Default: the Python
    currently on PATH.

.PARAMETER WorkingDir
    Path to the gateway source checkout. Default: the parent of this
    script's directory.

.EXAMPLE
    .\install-service.ps1

.EXAMPLE
    .\install-service.ps1 -Port 8443 -PythonExe C:\Python311\python.exe
#>

[CmdletBinding()]
param(
    [string]$ServiceName = 'FITTGateway',
    [int]$Port = 8080,
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
    # Script lives in <repo>\scripts; repo root is the parent.
    Split-Path -Parent $PSScriptRoot
}

function Resolve-DefaultPython {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw "python.exe not found on PATH. Pass -PythonExe explicitly or install Python 3.11+."
    }
    return $cmd.Source
}

function Assert-NssmPresent {
    $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw @"
NSSM is required but not on PATH.
Install via one of:
  choco install nssm
  scoop install nssm
  Download: https://nssm.cc/download
"@
    }
    return $cmd.Source
}

function Assert-FittHome {
    $fittHome = Join-Path $env:USERPROFILE '.fitt'
    if (-not (Test-Path $fittHome)) {
        throw @"
~/.fitt does not exist. Before installing the service:
  1. mkdir $fittHome
  2. Copy configs/config.example.yaml  -> $fittHome\config.yaml
  3. Copy configs/secrets.example.yaml -> $fittHome\secrets.yaml
  4. Edit both to fill in real values.
"@
    }
    foreach ($f in @('config.yaml', 'secrets.yaml')) {
        if (-not (Test-Path (Join-Path $fittHome $f))) {
            throw "Missing $fittHome\$f - copy from configs/$($f -replace '\.yaml$', '.example.yaml') first."
        }
    }
    return $fittHome
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

function Install-FittService {
    param(
        [string]$Name,
        [string]$Nssm,
        [string]$Python,
        [string]$RepoRoot,
        [string]$FittHome,
        [int]$Port
    )

    $logDir = Join-Path $FittHome 'logs'
    New-Item -ItemType Directory -Force $logDir | Out-Null

    Write-Step "Registering service '$Name'."
    # We run `python -m gateway`, which is the same code path as
    # `fitt-gateway` but avoids depending on the console script path
    # being present in the service's PATH.
    & $Nssm install $Name $Python '-m' 'gateway' | Out-Null
    & $Nssm set $Name AppDirectory (Join-Path $RepoRoot 'gateway') | Out-Null
    & $Nssm set $Name DisplayName 'FITT Gateway' | Out-Null
    & $Nssm set $Name Description 'FITT - OpenAI-compatible HTTP gateway for local and cloud LLMs.' | Out-Null
    & $Nssm set $Name Start SERVICE_AUTO_START | Out-Null

    # Restart policy: if the process exits, wait 30s and try again.
    & $Nssm set $Name AppExit Default Restart | Out-Null
    & $Nssm set $Name AppRestartDelay 30000 | Out-Null
    & $Nssm set $Name AppThrottle 5000 | Out-Null

    # Log NSSM's own stdout/stderr (not the gateway's - that's handled
    # by structlog) to files under ~/.fitt/logs.
    & $Nssm set $Name AppStdout (Join-Path $logDir 'service.stdout.log') | Out-Null
    & $Nssm set $Name AppStderr (Join-Path $logDir 'service.stderr.log') | Out-Null
    & $Nssm set $Name AppStdoutCreationDisposition 4 | Out-Null  # append
    & $Nssm set $Name AppStderrCreationDisposition 4 | Out-Null
    & $Nssm set $Name AppRotateFiles 1 | Out-Null
    & $Nssm set $Name AppRotateOnline 1 | Out-Null
    & $Nssm set $Name AppRotateBytes 10485760 | Out-Null  # 10 MB

    # Environment.
    $envBlock = "FITT_HOME=$FittHome`0" + "PYTHONUNBUFFERED=1`0"
    & $Nssm set $Name AppEnvironmentExtra $envBlock | Out-Null

    Write-Step "Starting service."
    & $Nssm start $Name | Out-Null
    Start-Sleep -Seconds 3
}

function Ensure-FirewallRule {
    param([int]$Port, [string]$Name = 'FITT Gateway (Private only)')

    # Remove any prior rule with the same name so re-runs are idempotent.
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

function Test-Gateway {
    param([int]$Port)
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" `
            -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) {
            Write-Host "[ok] /health responded 200." -ForegroundColor Green
        } else {
            Write-Warning "/health responded $($r.StatusCode); check the service log."
        }
    } catch {
        Write-Warning "/health did not respond yet: $_. The service may still be starting - check ~/.fitt/logs."
    }
}

# ---------------------------------------------------------------- main

Assert-Elevated

if (-not $WorkingDir) { $WorkingDir = Resolve-DefaultWorkingDir }
if (-not $PythonExe)  { $PythonExe  = Resolve-DefaultPython }
$nssm = Assert-NssmPresent
$fittHome = Assert-FittHome

Write-Step "Installing FITT Gateway service."
Write-Host "  ServiceName:  $ServiceName"
Write-Host "  Port:         $Port"
Write-Host "  PythonExe:    $PythonExe"
Write-Host "  WorkingDir:   $WorkingDir"
Write-Host "  FittHome:     $fittHome"
Write-Host "  NSSM:         $nssm"

Stop-ExistingService -Name $ServiceName
Install-FittService `
    -Name $ServiceName `
    -Nssm $nssm `
    -Python $PythonExe `
    -RepoRoot $WorkingDir `
    -FittHome $fittHome `
    -Port $Port

Ensure-FirewallRule -Port $Port
Test-Gateway -Port $Port

Write-Host ""
Write-Host "Service '$ServiceName' installed and started." -ForegroundColor Green
Write-Host "Verify with:  Get-Service $ServiceName"
Write-Host "Or:           curl http://localhost:$Port/health"
Write-Host "Logs:         $fittHome\logs\"
