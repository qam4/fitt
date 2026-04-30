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
    as Private). The Public profile remains blocked.

    The gateway is executed by the Python inside the repo's venv at
    gateway\.venv\Scripts\python.exe. The venv is managed by uv.

    Run from an elevated PowerShell prompt.

.PARAMETER ServiceName
    Name of the Windows service. Default: "FITTGateway".

.PARAMETER Port
    TCP port the gateway listens on. Default: 8080.

.PARAMETER SetupVenv
    When set, runs `uv sync` in the gateway directory before
    registering the service. Creates the venv and installs deps if
    missing; updates deps if present. Requires uv to be on PATH.

.PARAMETER PythonExe
    Escape hatch for users who manage their own Python environment.
    Must point at a Python interpreter where `pip install -e .` has
    already been run against the gateway package. For the default
    `uv sync`-managed flow, leave this unset.

.PARAMETER WorkingDir
    Path to the repo root. Default: the parent of this script's
    directory.

.EXAMPLE
    # Standard install: uv sync creates the venv, then register service.
    .\install-service.ps1 -SetupVenv

.EXAMPLE
    # Install against an already-synced venv (from a prior `uv sync`).
    .\install-service.ps1

.EXAMPLE
    # Advanced: point at a Python you manage yourself.
    .\install-service.ps1 -PythonExe C:\envs\fitt\Scripts\python.exe
#>

[CmdletBinding()]
param(
    [string]$ServiceName = 'FITTGateway',
    [int]$Port = 8080,
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
    # Script lives in <repo>\scripts; repo root is the parent.
    Split-Path -Parent $PSScriptRoot
}

function Get-VenvPython {
    param([string]$RepoRoot)
    Join-Path $RepoRoot 'gateway\.venv\Scripts\python.exe'
}

function Invoke-UvSync {
    param([string]$RepoRoot)

    # uv sync creates gateway\.venv\ if missing, downloads a
    # compatible Python interpreter if needed, and installs the
    # gateway package plus its dependencies from pyproject.toml.
    # Idempotent: re-running on an already-synced venv is fast.
    $uv = Get-Command uv.exe -ErrorAction SilentlyContinue
    if (-not $uv) {
        throw @"
-SetupVenv requires uv on PATH. Install uv with:
    winget install --id=astral-sh.uv -e
or:
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
Then re-run this script.
"@
    }
    Write-Step "Running 'uv sync' in gateway\"
    Push-Location (Join-Path $RepoRoot 'gateway')
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
    param(
        [string]$RepoRoot,
        [string]$ExplicitPythonExe,
        [bool]$DoSetupVenv
    )

    # Explicit -PythonExe wins. Used only by advanced users who manage
    # their own environment.
    if ($ExplicitPythonExe) {
        if (-not (Test-Path $ExplicitPythonExe)) {
            throw "Python interpreter not found at: $ExplicitPythonExe"
        }
        return $ExplicitPythonExe
    }

    # Otherwise the contract is: use gateway\.venv\Scripts\python.exe.
    # The -SetupVenv path guarantees it exists; without the flag we
    # require the user to have run `uv sync` themselves already.
    if ($DoSetupVenv) {
        Invoke-UvSync -RepoRoot $RepoRoot
    }

    $venvPython = Get-VenvPython -RepoRoot $RepoRoot
    if (-not (Test-Path $venvPython)) {
        throw @"
Expected Python at:
    $venvPython

Run 'uv sync' in the gateway\ directory first, or re-run this script
with -SetupVenv to do it automatically. See docs\quickstart.md.
"@
    }
    return $venvPython
}

function Assert-NssmPresent {
    $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw @"
NSSM is required but not on PATH.
Install via one of:
  winget install NSSM.NSSM
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
~\.fitt does not exist. Before installing the service:
  1. mkdir $fittHome
  2. Copy configs\config.example.yaml  -> $fittHome\config.yaml
  3. Copy configs\secrets.example.yaml -> $fittHome\secrets.yaml
  4. Edit both to fill in real values.
  5. icacls "$fittHome\secrets.yaml" /inheritance:r /grant:r "`$(`$env:USERNAME):F"
"@
    }
    foreach ($f in @('config.yaml', 'secrets.yaml')) {
        if (-not (Test-Path (Join-Path $fittHome $f))) {
            throw "Missing $fittHome\$f - copy from configs\$($f -replace '\.yaml$', '.example.yaml') first."
        }
    }
    return $fittHome
}

function Assert-GatewayImportable {
    param([string]$Python)

    # Catch the single most common install-time failure up front: the
    # Python we're about to register cannot `import gateway`. Without
    # this check, NSSM would register the service, try to start it,
    # the process would exit immediately, and the user would see only
    # SERVICE_PAUSED with nothing obvious in the logs.
    & $Python -c "import gateway; print(gateway.__version__)" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw @"
The Python interpreter at:
    $Python
cannot import the 'gateway' package.

If you used -SetupVenv, something went wrong during uv sync; check
the output above.

If you passed -PythonExe manually, run `pip install -e .` into that
environment against the gateway\ directory.
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
    # Run `python -m gateway` rather than the `fitt-gateway` console
    # script. Both work, but invoking the module avoids depending on
    # the Scripts\ dir being on the service's PATH.
    & $Nssm install $Name $Python '-m' 'gateway' | Out-Null
    & $Nssm set $Name AppDirectory (Join-Path $RepoRoot 'gateway') | Out-Null
    & $Nssm set $Name DisplayName 'FITT Gateway' | Out-Null
    & $Nssm set $Name Description 'FITT - OpenAI-compatible HTTP gateway for local and cloud LLMs.' | Out-Null
    & $Nssm set $Name Start SERVICE_AUTO_START | Out-Null

    # Restart policy: if the process exits, wait 30s and try again.
    & $Nssm set $Name AppExit Default Restart | Out-Null
    & $Nssm set $Name AppRestartDelay 30000 | Out-Null
    & $Nssm set $Name AppThrottle 5000 | Out-Null

    # NSSM's own stdout/stderr capture (the gateway writes its own
    # structured logs via structlog to gateway.log).
    & $Nssm set $Name AppStdout (Join-Path $logDir 'service.stdout.log') | Out-Null
    & $Nssm set $Name AppStderr (Join-Path $logDir 'service.stderr.log') | Out-Null
    & $Nssm set $Name AppStdoutCreationDisposition 4 | Out-Null  # append
    & $Nssm set $Name AppStderrCreationDisposition 4 | Out-Null
    & $Nssm set $Name AppRotateFiles 1 | Out-Null
    & $Nssm set $Name AppRotateOnline 1 | Out-Null
    & $Nssm set $Name AppRotateBytes 10485760 | Out-Null  # 10 MB

    # Service environment. FITT_HOME pins the config/secrets
    # directory; PYTHONUNBUFFERED makes stdout/stderr flush promptly
    # so NSSM's capture files stay current.
    $envBlock = "FITT_HOME=$FittHome`0" + "PYTHONUNBUFFERED=1`0"
    & $Nssm set $Name AppEnvironmentExtra $envBlock | Out-Null

    Write-Step "Starting service."
    & $Nssm start $Name | Out-Null
    Start-Sleep -Seconds 3
}

function Ensure-FirewallRule {
    param([int]$Port, [string]$Name = 'FITT Gateway (Private only)')

    # Remove any prior rule with the same display name so re-runs are
    # idempotent.
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
    param([int]$Port, [int]$TimeoutSeconds = 45)

    # On first boot Python imports litellm, pydantic, etc. which can
    # take 15-30 seconds. Poll /health for up to the timeout before
    # warning.
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastError = $null
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" `
                -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200) {
                Write-Host "[ok] /health responded 200." -ForegroundColor Green
                return
            }
            $lastError = "status=$($r.StatusCode)"
        } catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Seconds 2
    }
    Write-Warning "/health did not respond within ${TimeoutSeconds}s (last error: $lastError)."
    Write-Warning "The service may still be starting slowly. Check ~\.fitt\logs\gateway.log and try curl http://localhost:$Port/health in a moment."
}

# ---------------------------------------------------------------- main

Assert-Elevated

if (-not $WorkingDir) { $WorkingDir = Resolve-DefaultWorkingDir }
$python = Resolve-Python -RepoRoot $WorkingDir `
                        -ExplicitPythonExe $PythonExe `
                        -DoSetupVenv:$SetupVenv
$nssm = Assert-NssmPresent
$fittHome = Assert-FittHome
Assert-GatewayImportable -Python $python

Write-Step "Installing FITT Gateway service."
Write-Host "  ServiceName:  $ServiceName"
Write-Host "  Port:         $Port"
Write-Host "  PythonExe:    $python"
Write-Host "  WorkingDir:   $WorkingDir"
Write-Host "  FittHome:     $fittHome"
Write-Host "  NSSM:         $nssm"

Stop-ExistingService -Name $ServiceName
Install-FittService `
    -Name $ServiceName `
    -Nssm $nssm `
    -Python $python `
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
