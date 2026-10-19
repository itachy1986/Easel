param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Start', 'Stop')]
    [string]$Action,

    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path.TrimEnd(
    [IO.Path]::DirectorySeparatorChar,
    [IO.Path]::AltDirectorySeparatorChar)
$ListenHost = '127.0.0.1'
$ListenPort = 7860
$RuntimeDir = Join-Path $ProjectRoot 'outputs\_launcher'
$RecordPath = Join-Path $RuntimeDir 'web-7860.json'
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$StdoutPath = Join-Path $RuntimeDir 'web-7860.stdout.log'
$StderrPath = Join-Path $RuntimeDir 'web-7860.stderr.log'

function Write-Status([string]$Message) {
    Write-Host "[Easel] $Message"
}

function Fail([string]$Message) {
    throw "[Easel] $Message"
}

function Get-ListenerProcessId {
    try {
        $rows = @(Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction Stop)
        if ($rows.Count -gt 1) {
            Fail "Multiple listeners were reported for port $ListenPort; refusing to continue."
        }
        if ($rows.Count -eq 1) {
            return [int]$rows[0].OwningProcess
        }
    }
    catch {
        # AccessDenied and no-result errors are not treated as proof that the port is free.
    }

    $lines = @(& "$env:SystemRoot\System32\netstat.exe" -ano -p tcp)
    if ($LASTEXITCODE -ne 0) {
        Fail 'Unable to verify the local listener with Get-NetTCPConnection or netstat.'
    }
    $matches = @($lines | Where-Object {
        $_ -match '^\s*TCP\s+127\.0\.0\.1:7860\s+\S+\s+LISTENING\s+(\d+)\s*$'
    })
    if ($matches.Count -gt 1) {
        Fail "Multiple loopback listeners were reported for port $ListenPort."
    }
    if ($matches.Count -eq 0) {
        return $null
    }
    [void]($matches[0] -match '(\d+)\s*$')
    return [int]$Matches[1]
}

function Get-ProcessIdentity([int]$TargetProcessId) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$TargetProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $null
    }
    $created = (Get-Date $process.CreationDate).ToUniversalTime().ToString('o')
    return [pscustomobject]@{
        ProcessId = [int]$process.ProcessId
        ParentProcessId = [int]$process.ParentProcessId
        CreationDate = $created
        ExecutablePath = [string]$process.ExecutablePath
        CommandLine = [string]$process.CommandLine
    }
}

function Test-SamePath([string]$Left, [string]$Right) {
    if ([string]::IsNullOrWhiteSpace($Left) -or [string]::IsNullOrWhiteSpace($Right)) {
        return $false
    }
    return [string]::Equals(
        [IO.Path]::GetFullPath($Left),
        [IO.Path]::GetFullPath($Right),
        [StringComparison]::OrdinalIgnoreCase)
}

function Test-CommandIdentity([string]$CommandLine) {
    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        return $false
    }
    return $CommandLine.IndexOf('web.app:app', [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $CommandLine.IndexOf('--host 127.0.0.1', [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $CommandLine.IndexOf('--port 7860', [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $CommandLine.IndexOf($ProjectRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0
}

function Get-Readiness {
    try {
        $homepage = Invoke-WebRequest -Uri "http://${ListenHost}:${ListenPort}/" -UseBasicParsing -TimeoutSec 3
        $settings = Invoke-WebRequest -Uri "http://${ListenHost}:${ListenPort}/api/settings/harness" -UseBasicParsing -TimeoutSec 3
        if ($homepage.StatusCode -ne 200 -or $settings.StatusCode -ne 200) {
            return $null
        }
        $payload = $settings.Content | ConvertFrom-Json
        $codex = @($payload.agents | Where-Object { $_.id -eq 'codex' }) | Select-Object -First 1
        return [pscustomobject]@{
            HomepageStatus = [int]$homepage.StatusCode
            SettingsStatus = [int]$settings.StatusCode
            CodexInstalled = ($null -ne $codex -and [bool]$codex.installed)
        }
    }
    catch {
        return $null
    }
}

function Read-Record {
    if (-not (Test-Path -LiteralPath $RecordPath -PathType Leaf)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $RecordPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        Fail 'The launcher ownership record is unreadable; refusing to guess process ownership.'
    }
}

function Assert-RecordIdentity($record, [int]$CurrentListenerProcessId) {
    if (-not (Test-SamePath ([string]$record.ProjectRoot) $ProjectRoot)) {
        Fail 'The launcher record belongs to a different project root.'
    }
    if ([int]$record.Port -ne $ListenPort -or [string]$record.Host -ne $ListenHost) {
        Fail 'The launcher record belongs to a different host or port.'
    }
    if ([int]$record.ListenerPid -ne $CurrentListenerProcessId) {
        Fail 'Port 7860 is owned by a different process; refusing to stop it.'
    }

    $launcher = Get-ProcessIdentity ([int]$record.LauncherPid)
    $listener = Get-ProcessIdentity ([int]$record.ListenerPid)
    if ($null -eq $launcher -or $null -eq $listener) {
        Fail 'The recorded process tree is incomplete; refusing to stop any process.'
    }
    if ($listener.ParentProcessId -ne [int]$record.LauncherPid) {
        Fail 'The listener is no longer a child of the recorded launcher.'
    }
    if ($launcher.CreationDate -ne [string]$record.LauncherCreationDate -or
        $listener.CreationDate -ne [string]$record.ListenerCreationDate) {
        Fail 'A recorded PID has been reused; refusing to stop it.'
    }
    if (-not (Test-SamePath $launcher.ExecutablePath ([string]$record.LauncherExecutable)) -or
        -not (Test-SamePath $listener.ExecutablePath ([string]$record.ListenerExecutable))) {
        Fail 'A recorded executable path changed; refusing to stop it.'
    }
    if (-not (Test-SamePath $launcher.ExecutablePath $PythonPath)) {
        Fail 'The recorded launcher is not this project venv Python.'
    }
    if (-not (Test-CommandIdentity $launcher.CommandLine) -or
        -not (Test-CommandIdentity $listener.CommandLine)) {
        Fail 'The recorded command no longer matches this project and loopback port.'
    }
    return [pscustomobject]@{ Launcher = $launcher; Listener = $listener }
}

function Start-Easel {
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        Fail "Project venv Python is missing at .venv\Scripts\python.exe."
    }
    New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null

    $existingListenerProcessId = Get-ListenerProcessId
    $existingRecord = Read-Record
    if ($null -ne $existingListenerProcessId) {
        if ($null -eq $existingRecord) {
            Fail 'Port 7860 is already occupied without an Easel ownership record.'
        }
        [void](Assert-RecordIdentity $existingRecord $existingListenerProcessId)
        $existingReadiness = Get-Readiness
        if ($null -eq $existingReadiness) {
            Fail 'The recorded Easel process owns port 7860 but is not HTTP-ready.'
        }
        Write-Status "Already running at http://${ListenHost}:${ListenPort}/"
        Write-Status "HTTP home/settings: 200/200; Codex installed: $($existingReadiness.CodexInstalled)"
        return
    }
    if ($null -ne $existingRecord) {
        $recordedLauncher = Get-ProcessIdentity ([int]$existingRecord.LauncherPid)
        if ($null -ne $recordedLauncher) {
            Fail 'A recorded launcher is still alive without the expected listener; refusing a duplicate start.'
        }
        Remove-Item -LiteralPath $RecordPath -Force
    }

    $quotedRoot = '"' + $ProjectRoot + '"'
    $arguments = @(
        '-m', 'uvicorn', 'web.app:app',
        '--app-dir', $quotedRoot,
        '--host', $ListenHost,
        '--port', [string]$ListenPort
    )
    $launcherProcess = Start-Process -FilePath $PythonPath -ArgumentList $arguments `
        -WorkingDirectory $ProjectRoot -RedirectStandardOutput $StdoutPath `
        -RedirectStandardError $StderrPath -WindowStyle Hidden -PassThru

    $readiness = $null
    $listenerProcessId = $null
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        Start-Sleep -Milliseconds 500
        $listenerProcessId = Get-ListenerProcessId
        if ($null -ne $listenerProcessId) {
            $readiness = Get-Readiness
            if ($null -ne $readiness) {
                break
            }
        }
        if ($launcherProcess.HasExited) {
            break
        }
    }
    if ($null -eq $listenerProcessId -or $null -eq $readiness) {
        Fail 'Easel did not become ready within 30 seconds. Review the private outputs/_launcher logs.'
    }

    $launcherIdentity = Get-ProcessIdentity $launcherProcess.Id
    $listenerIdentity = Get-ProcessIdentity $listenerProcessId
    if ($null -eq $launcherIdentity -or $null -eq $listenerIdentity -or
        $listenerIdentity.ParentProcessId -ne $launcherProcess.Id -or
        -not (Test-SamePath $launcherIdentity.ExecutablePath $PythonPath) -or
        -not (Test-CommandIdentity $launcherIdentity.CommandLine) -or
        -not (Test-CommandIdentity $listenerIdentity.CommandLine)) {
        Fail 'HTTP became ready, but exact launcher/listener ownership could not be proven.'
    }

    $record = [ordered]@{
        Version = 1
        ProjectRoot = $ProjectRoot
        Host = $ListenHost
        Port = $ListenPort
        LauncherPid = $launcherIdentity.ProcessId
        LauncherCreationDate = $launcherIdentity.CreationDate
        LauncherExecutable = $launcherIdentity.ExecutablePath
        ListenerPid = $listenerIdentity.ProcessId
        ListenerCreationDate = $listenerIdentity.CreationDate
        ListenerExecutable = $listenerIdentity.ExecutablePath
    }
    $temporaryRecord = "$RecordPath.tmp"
    $record | ConvertTo-Json | Set-Content -LiteralPath $temporaryRecord -Encoding UTF8
    Move-Item -LiteralPath $temporaryRecord -Destination $RecordPath -Force

    Write-Status "Running at http://${ListenHost}:${ListenPort}/"
    Write-Status "HTTP home/settings: 200/200; Codex installed: $($readiness.CodexInstalled)"
    Write-Status 'Private logs and ownership record: outputs\_launcher'
}

function Stop-Easel {
    $currentListenerProcessId = Get-ListenerProcessId
    $record = Read-Record
    if ($null -eq $record) {
        if ($null -eq $currentListenerProcessId) {
            Write-Status 'Already stopped; port 7860 is free.'
            return
        }
        Fail 'Port 7860 is occupied without an Easel ownership record; refusing to stop it.'
    }
    if ($null -eq $currentListenerProcessId) {
        $recordedLauncher = Get-ProcessIdentity ([int]$record.LauncherPid)
        if ($null -ne $recordedLauncher) {
            Fail 'The recorded launcher is still alive but the expected listener is absent.'
        }
        Remove-Item -LiteralPath $RecordPath -Force
        Write-Status 'Already stopped; removed a stale ownership record.'
        return
    }

    $identity = Assert-RecordIdentity $record $currentListenerProcessId
    Stop-Process -Id $record.ListenerPid -ErrorAction Stop

    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 500
        if ($null -eq (Get-ListenerProcessId)) {
            break
        }
    }
    $remainingListenerProcessId = Get-ListenerProcessId
    if ($null -ne $remainingListenerProcessId) {
        Fail 'The verified listener did not stop; ownership record was preserved.'
    }

    $remainingLauncher = Get-ProcessIdentity ([int]$record.LauncherPid)
    if ($null -ne $remainingLauncher) {
        if ($remainingLauncher.CreationDate -ne [string]$record.LauncherCreationDate -or
            -not (Test-SamePath $remainingLauncher.ExecutablePath ([string]$record.LauncherExecutable)) -or
            -not (Test-CommandIdentity $remainingLauncher.CommandLine)) {
            Fail 'Launcher PID identity changed after listener exit; refusing to stop it.'
        }
        Stop-Process -Id $record.LauncherPid -ErrorAction Stop
    }
    Remove-Item -LiteralPath $RecordPath -Force
    Write-Status 'Stopped the verified Easel 7860 process tree. No other listener was touched.'
}

try {
    if ($Action -eq 'Start') {
        Start-Easel
    }
    else {
        Stop-Easel
    }
    exit 0
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
