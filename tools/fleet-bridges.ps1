#Requires -Version 5.1
<#
fleet-bridges.ps1 -- Windows launcher for the FleetKit bridges.

Why this exists: install.sh registers launchd agents (macOS) for every bridge.
On Windows there is no launchd, so the bridges were simply never started while
opencodex (127.0.0.1:10100) already advertises them as providers. Every fleet
model then fails with:

    unexpected status 502 Bad Gateway: Provider unreachable: Unable to connect.
    Is the computer able to access the url?, url: http://127.0.0.1:10100/v1/responses

Usage:
    pwsh -ExecutionPolicy Bypass -File .\fleet-bridges.ps1 status
    pwsh -ExecutionPolicy Bypass -File .\fleet-bridges.ps1 start
    pwsh -ExecutionPolicy Bypass -File .\fleet-bridges.ps1 restart -Only trae,workbuddy
    pwsh -ExecutionPolicy Bypass -File .\fleet-bridges.ps1 stop
    pwsh -ExecutionPolicy Bypass -File .\fleet-bridges.ps1 install-task   # start at logon
    pwsh -ExecutionPolicy Bypass -File .\fleet-bridges.ps1 remove-task
#>
[CmdletBinding()]
param(
    [ValidateSet('start', 'stop', 'restart', 'status', 'install-task', 'remove-task')]
    [string]$Action = 'status',
    [string[]]$Only = @(),
    [string]$FleetHome = $env:FLEET_HOME
)

$ErrorActionPreference = 'Stop'
$TASK_NAME = 'FleetKitBridges'

# name | bridge-dir | script | port-offset | key-env | uses --host/--port args | extra env
$BRIDGES = @(
    @{ Name = 'workbuddy';     Dir = 'workbuddy-cn';  Script = 'converter.py';                Offset = 0;  Key = 'CODEBUDDY2OPENAI_KEY'; PortArg = $true;  Env = @() },
    @{ Name = 'workbuddy-gpt'; Dir = 'workbuddy-gpt'; Script = 'converter.py';                Offset = 1;  Key = 'CODEBUDDY2OPENAI_KEY'; PortArg = $true;  Env = @('WORKBUDDY_AUTH_POOL_DIR=@FLEET_HOME@/bridges/workbuddy-gpt/auths', 'WORKBUDDY_LOCAL_STORAGE=@HOME@/.workbuddy-ai/local_storage') },
    @{ Name = 'qoder';         Dir = 'qoder';         Script = 'qoder_bridge.py';             Offset = 2;  Key = 'QODER2CODEX_KEY';       PortArg = $true;  Env = @('QODER_CALL_TIMEOUT=300') },
    @{ Name = 'codely';        Dir = 'codely';        Script = 'codely_bridge.py';            Offset = 3;  Key = 'CODELY2CODEX_KEY';      PortArg = $true;  Env = @('CODELY_CALL_TIMEOUT=300') },
    @{ Name = 'trae';          Dir = 'trae';          Script = 'trae_bridge.py';              Offset = 4;  Key = 'TRAE2CODEX_KEY';        PortArg = $true;  Env = @('TRAE_CALL_TIMEOUT=300') },
    @{ Name = 'lingxi';        Dir = 'lingxi';        Script = 'lingxi_bridge.py';            Offset = 5;  Key = 'LINGXI2CODEX_KEY';      PortArg = $true;  Env = @('LINGXI_CALL_TIMEOUT=300') },
    @{ Name = 'xhx';           Dir = 'xhx';           Script = 'xhx_bridge.py';               Offset = 6;  Key = 'XHX2CODEX_KEY';         PortArg = $true;  Env = @('XHX_CALL_TIMEOUT=300') },
    @{ Name = 'gemini';        Dir = 'gemini';        Script = 'gemini_bridge.py';            Offset = 7;  Key = 'GEMINI2CODEX_KEY';      PortArg = $false; Env = @('GEMINI2CODEX_PORT=@PORT@') },
    @{ Name = 'catpaw';        Dir = 'catpaw';        Script = 'catpaw_bridge.py';            Offset = 8;  Key = 'CATPAW2CODEX_KEY';      PortArg = $false; Env = @('CATPAW_PORT=@PORT@') },
    @{ Name = 'antigravity';   Dir = 'antigravity';   Script = 'antigravity_bridge.py';       Offset = 10; Key = 'ANTIGRAVITY2CODEX_KEY'; PortArg = $false; Env = @('ANTIGRAVITY2CODEX_PORT=@PORT@', 'ANTIGRAVITY2CODEX_HOST=127.0.0.1') },
    @{ Name = 'qwen';          Dir = 'qwen';          Script = 'qwen_bridge.py';              Offset = 11; Key = 'QWEN2CODEX_KEY';        PortArg = $true;  Env = @('QWEN_CALL_TIMEOUT=300') },
    @{ Name = 'cline';         Dir = 'cline';         Script = 'cline_bridge.py';             Offset = 12; Key = 'CLINE2CODEX_KEY';       PortArg = $true;  Env = @('CLINE_CALL_TIMEOUT=300') },
    @{ Name = 'zcode';         Dir = 'zcode';         Script = 'zcode_bridge.py';             Offset = 13; Key = 'ZCODE2CODEX_KEY';       PortArg = $true;  Env = @('ZCODE_CALL_TIMEOUT=300') }
)

function Resolve-FleetHome {
    param([string]$Value)
    if ($Value -and (Test-Path $Value)) { return $Value }
    if ($env:FLEET_HOME -and (Test-Path $env:FLEET_HOME)) { return $env:FLEET_HOME }
    foreach ($cand in @((Join-Path $env:USERPROFILE 'FleetKit\runtime'), (Join-Path $env:USERPROFILE 'FleetKit'))) {
        if (Test-Path $cand) { return $cand }
    }
    return (Join-Path $env:USERPROFILE 'FleetKit\runtime')
}

function Read-FleetEnv {
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path $Path)) { return $map }
    foreach ($line in Get-Content -Path $Path) {
        if ($line -match '^\s*(#|$)') { continue }
        $idx = $line.IndexOf('=')
        if ($idx -lt 1) { continue }
        $k = $line.Substring(0, $idx).Trim()
        $v = $line.Substring($idx + 1).Trim().Trim('"').Trim("'")
        $map[$k] = $v
    }
    return $map
}

function Resolve-LogDir {
    param([string]$From, [string]$FleetHome)
    $dir = $From
    if (-not $dir -or $dir -match '^/tmp' -or $dir -notmatch '^[A-Za-z]:') {
        $dir = Join-Path $FleetHome 'logs'
    }
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    return $dir
}

function Resolve-Python {
    param([hashtable]$EnvMap, [string]$FleetHome)
    foreach ($cand in @($EnvMap['FLEET_PYTHON'], (Join-Path $FleetHome '.venv\Scripts\python.exe'))) {
        if ($cand -and $cand -match '^[A-Za-z]:' -and (Test-Path $cand)) { return $cand }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw 'no python interpreter found (set FLEET_PYTHON in fleet.env)'
}

function Test-PortListening {
    param([int]$Port)
    $c = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
    return [bool]$c
}

function Get-PortOwner {
    param([int]$Port)
    $c = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return $c.OwningProcess }
    return $null
}

function Get-Specs {
    param([hashtable]$EnvMap, [string]$FleetHome, [string]$Python, [string[]]$Only)
    $portBase = 8787
    if ($EnvMap['PORT_BASE'] -and $EnvMap['PORT_BASE'] -match '^\d+$') { $portBase = [int]$EnvMap['PORT_BASE'] }
    $specs = @()
    foreach ($b in $BRIDGES) {
        if ($Only.Count -gt 0 -and $Only -notcontains $b.Name) { continue }
        $port = $portBase + $b.Offset
        $scriptPath = Join-Path $FleetHome "bridges\$($b.Dir)\$($b.Script)"
        if (-not (Test-Path $scriptPath)) { continue }
        $keyValue = $EnvMap[$b.Key]
        if (-not $keyValue) { continue }
        $args = @($scriptPath)
        if ($b.PortArg) { $args += @('--host', '127.0.0.1', '--port', "$port") }
        $extra = @()
        foreach ($entry in $b.Env) {
            $v = $entry -replace '@PORT@', "$port" `
                        -replace '@FLEET_HOME@', $FleetHome `
                        -replace '@HOME@', $env:USERPROFILE
            $extra += $v
        }
        if ($b.Name -eq 'antigravity') {
            if ($EnvMap['ANTIGRAVITY_OAUTH_CLIENT_ID']) { $extra += "ANTIGRAVITY_OAUTH_CLIENT_ID=$($EnvMap['ANTIGRAVITY_OAUTH_CLIENT_ID'])" }
            if ($EnvMap['ANTIGRAVITY_OAUTH_CLIENT_SECRET']) { $extra += "ANTIGRAVITY_OAUTH_CLIENT_SECRET=$($EnvMap['ANTIGRAVITY_OAUTH_CLIENT_SECRET'])" }
            if ($EnvMap['ANTIGRAVITY_LEGACY_CLIENTS']) { $extra += "ANTIGRAVITY_LEGACY_CLIENTS=$($EnvMap['ANTIGRAVITY_LEGACY_CLIENTS'])" }
        }
        $specs += [pscustomobject]@{
            Name       = $b.Name
            Port       = $port
            WorkDir    = Join-Path $FleetHome "bridges\$($b.Dir)"
            Python     = $Python
            Args       = $args
            KeyEnv     = "$($b.Key)=$keyValue"
            ExtraEnv   = $extra
        }
    }
    return $specs
}

function Start-Bridge {
    param($Spec, [string]$LogDir)
    if (Test-PortListening -Port $Spec.Port) {
        Write-Host ("  [skip ] {0,-14} :{1} already listening" -f $Spec.Name, $Spec.Port)
        return
    }
    $outLog = Join-Path $LogDir "$($Spec.Name).out.log"
    $errLog = Join-Path $LogDir "$($Spec.Name).err.log"
    $pidFile = Join-Path $LogDir "$($Spec.Name).pid"

    $saved = @{}
    foreach ($pair in (@("HOME=$($env:USERPROFILE)", $Spec.KeyEnv) + $Spec.ExtraEnv)) {
        $k, $v = $pair -split '=', 2
        $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process')
        Set-Item -Path "Env:$k" -Value $v
    }
    try {
        $proc = Start-Process -FilePath $Spec.Python -ArgumentList $Spec.Args `
            -WorkingDirectory $Spec.WorkDir -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $outLog -RedirectStandardError $errLog
        Set-Content -Path $pidFile -Value $proc.Id -Encoding ASCII
    }
    finally {
        foreach ($k in $saved.Keys) {
            if ($null -eq $saved[$k]) { Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue }
            else { Set-Item -Path "Env:$k" -Value $saved[$k] }
        }
    }

    $ok = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 250
        if (Test-PortListening -Port $Spec.Port) { $ok = $true; break }
    }
    if ($ok) {
        Write-Host ("  [start] {0,-14} :{1} up (pid {2})" -f $Spec.Name, $Spec.Port, $proc.Id)
    }
    else {
        Write-Host ("  [FAIL ] {0,-14} :{1} did not bind; see {2}" -f $Spec.Name, $Spec.Port, $errLog) -ForegroundColor Yellow
        if (Test-Path $errLog) { Get-Content $errLog -Tail 5 | ForEach-Object { Write-Host "          $_" } }
    }
}

function Stop-Bridge {
    param($Spec, [string]$LogDir)
    $pidFile = Join-Path $LogDir "$($Spec.Name).pid"
    $killed = $false
    if (Test-Path $pidFile) {
        $procId = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($procId -and (Get-Process -Id $procId -ErrorAction SilentlyContinue)) {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            $killed = $true
        }
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    }
    $owner = Get-PortOwner -Port $Spec.Port
    if ($owner) {
        Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
        $killed = $true
    }
    if ($killed) { Write-Host ("  [stop ] {0,-14} :{1}" -f $Spec.Name, $Spec.Port) }
    else { Write-Host ("  [idle ] {0,-14} :{1} not running" -f $Spec.Name, $Spec.Port) }
}

function Show-Bridge {
    param($Spec)
    $up = Test-PortListening -Port $Spec.Port
    if ($up) { Write-Host ("  [up   ] {0,-14} :{1}" -f $Spec.Name, $Spec.Port) -ForegroundColor Green }
    else { Write-Host ("  [down ] {0,-14} :{1}" -f $Spec.Name, $Spec.Port) -ForegroundColor Red }
    return $up
}

$fleetHome = Resolve-FleetHome -Value $FleetHome
$envMap = Read-FleetEnv -Path (Join-Path $fleetHome 'fleet.env')
$logDir = Resolve-LogDir -From $envMap['LOG_DIR'] -FleetHome $fleetHome
$python = Resolve-Python -EnvMap $envMap -FleetHome $fleetHome

if ($Action -eq 'install-task' -or $Action -eq 'remove-task') {
    $scriptPath = $PSCommandPath
    if (-not $scriptPath) { $scriptPath = Join-Path $fleetHome 'tools\fleet-bridges.ps1' }
    if ($Action -eq 'install-task') {
        $wrapper = Join-Path $fleetHome 'start-bridges.cmd'
        Set-Content -Path $wrapper -Encoding ASCII -Value @(
            '@echo off',
            'set "PS=pwsh.exe"',
            'where pwsh.exe >nul 2>nul || set "PS=powershell.exe"',
            '"%PS%" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0tools\fleet-bridges.ps1" -Action start'
        )
        & schtasks /Create /TN $TASK_NAME /TR "`"$wrapper`"" /SC ONLOGON /F | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "installed logon task '$TASK_NAME' -> $wrapper"
        }
        else {
            # schtasks needs elevation; the per-user Startup folder does not.
            $startup = [Environment]::GetFolderPath('Startup')
            if (-not $startup) { $startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup' }
            if (-not (Test-Path $startup)) { New-Item -ItemType Directory -Path $startup -Force | Out-Null }
            $link = Join-Path $startup 'FleetKit Bridges.lnk'
            $ws = New-Object -ComObject WScript.Shell
            $sc = $ws.CreateShortcut($link)
            $psExe = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
            if (-not $psExe) { $psExe = 'powershell.exe' }
            $sc.TargetPath = $psExe
            $sc.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`" -Action start"
            $sc.WorkingDirectory = $fleetHome
            $sc.WindowStyle = 7
            $sc.Save()
            Write-Host "schtasks needs elevation; installed Startup shortcut instead -> $link"
        }
    }
    else {
        & schtasks /Delete /TN $TASK_NAME /F | Out-Null
        Write-Host "removed logon task '$TASK_NAME'"
    }
    exit 0
}

$specs = Get-Specs -EnvMap $envMap -FleetHome $fleetHome -Python $python -Only $Only
if (-not $specs -or $specs.Count -eq 0) { throw "no bridges matched (fleet home: $fleetHome)" }

switch ($Action) {
    'start' {
        Write-Host "starting bridges (fleet home: $fleetHome, python: $python)"
        foreach ($s in $specs) { Start-Bridge -Spec $s -LogDir $logDir }
    }
    'stop' {
        foreach ($s in $specs) { Stop-Bridge -Spec $s -LogDir $logDir }
    }
    'restart' {
        foreach ($s in $specs) { Stop-Bridge -Spec $s -LogDir $logDir; Start-Bridge -Spec $s -LogDir $logDir }
    }
    'status' {
        $up = 0
        foreach ($s in $specs) { if (Show-Bridge -Spec $s) { $up++ } }
        Write-Host ("{0}/{1} bridges listening; logs: {2}" -f $up, $specs.Count, $logDir)
    }
}
