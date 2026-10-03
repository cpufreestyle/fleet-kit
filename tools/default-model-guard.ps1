#Requires -Version 5.1
<#
default-model-guard.ps1 -- Windows wrapper for tools/default_model_guard.py.

Keeps `stepfun/step-5-preview` (FLEET_DEFAULT_MODEL) as the Codex default and
falls back to it whenever the currently pinned model stops answering:

    pwsh -ExecutionPolicy Bypass -File .\default-model-guard.ps1 run
    pwsh -ExecutionPolicy Bypass -File .\default-model-guard.ps1 install   # daemon at logon
    pwsh -ExecutionPolicy Bypass -File .\default-model-guard.ps1 remove
    pwsh -ExecutionPolicy Bypass -File .\default-model-guard.ps1 status
#>
[CmdletBinding()]
param(
    [ValidateSet('run', 'run-loop', 'install', 'remove', 'status')]
    [string]$Action = 'run',
    [string]$FleetHome = $env:FLEET_HOME,
    [double]$Interval = 120
)

$ErrorActionPreference = 'Stop'
$SHORTCUT = 'FleetKit Default Model Guard.lnk'

function Resolve-FleetHome {
    param([string]$Value)
    if ($Value -and (Test-Path $Value)) { return $Value }
    if ($env:FLEET_HOME -and (Test-Path $env:FLEET_HOME)) { return $env:FLEET_HOME }
    foreach ($c in @((Join-Path $env:USERPROFILE 'FleetKit\runtime'), (Join-Path $env:USERPROFILE 'FleetKit'))) {
        if (Test-Path $c) { return $c }
    }
    return (Join-Path $env:USERPROFILE 'FleetKit\runtime')
}

$fleetHome = Resolve-FleetHome -Value $FleetHome
$script = Join-Path $fleetHome 'tools\default_model_guard.py'
if (-not (Test-Path $script)) { throw "missing $script" }
$envFile = Join-Path $fleetHome 'fleet.env'

$python = Join-Path $fleetHome '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = (Get-Command python -ErrorAction Stop).Source }

$startup = [Environment]::GetFolderPath('Startup')
if (-not $startup) { $startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup' }
$link = Join-Path $startup $SHORTCUT

function Get-AnchorModel {
    $envFile = Join-Path $fleetHome 'fleet.env'
    if (Test-Path $envFile) {
        $hit = Get-Content -Encoding UTF8 $envFile | Where-Object { $_ -match '^\s*FLEET_DEFAULT_MODEL\s*=' } | Select-Object -First 1
        if ($hit) { return ($hit -split '=', 2)[1].Trim().Trim('"') }
    }
    return 'stepfun/step-5-preview'
}

switch ($Action) {
    # The guard script is single-shot: it probes the pinned model's own route
    # and jumps back to the harbor when dead, then exits. There is no --once
    # or --daemon flag; the loop below is the daemon.
    'run' {
        & $python $script --env-file $envFile
        exit $LASTEXITCODE
    }
    'run-loop' {
        while ($true) {
            & $python $script --env-file $envFile
            Start-Sleep -Seconds $Interval
        }
    }
    'install' {
        if (-not (Test-Path $startup)) { New-Item -ItemType Directory -Path $startup -Force | Out-Null }
        $ws = New-Object -ComObject WScript.Shell
        $sc = $ws.CreateShortcut($link)
        $psExe = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
        if (-not $psExe) { throw "PowerShell 7 (pwsh) not found; install it or set FLEET_POWERSHELL" }
        $sc.TargetPath = $psExe
        $sc.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" -Action run-loop -Interval $Interval"
        $sc.WorkingDirectory = $fleetHome
        $sc.WindowStyle = 7
        $sc.Save()
        Write-Host "installed logon daemon -> $link (every $Interval s)"
    }
    'remove' {
        if (Test-Path $link) { Remove-Item $link -Force; Write-Host "removed $link" }
        else { Write-Host "not installed" }
    }
    'status' {
        Write-Host ("startup daemon   : " + ($(if (Test-Path $link) { 'installed' } else { 'absent' })))
        Write-Host ("anchor model     : " + (Get-AnchorModel))
        $pin = Select-String -Path (Join-Path $env:USERPROFILE '.codex\config.toml') -Pattern '^\s*model\s*=' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($pin) { Write-Host ("pinned in codex  : " + $pin.Line.Trim()) }
        else { Write-Host "pinned in codex  : (no model= line found in ~/.codex/config.toml)" }
        & $python $script --env-file $envFile --dry-run
    }
}
