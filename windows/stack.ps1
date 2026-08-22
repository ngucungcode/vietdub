[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "restart", "status", "logs", "foreground")]
    [string]$Action = "status",
    [int]$Lines = 100
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$context = Initialize-DubWindowsEnvironment
New-DubWindowsDirectories -Context $context
$statePath = Join-Path $context.RunDirectory "windows-stack.json"

function Assert-Runtime {
    if (-not (Test-Path -LiteralPath $context.VenvPython -PathType Leaf)) {
        throw "Chua co runtime Windows; hay chay .\windows\install.ps1"
    }
}

function Read-StackState {
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        return $null
    }
    try {
        return (Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json)
    } catch {
        throw "State stack Windows bi hong: $statePath"
    }
}

function Get-ValidatedProcess {
    param($Entry)

    if ($null -eq $Entry -or $Entry.id -notmatch '^\d+$') {
        return $null
    }
    $process = Get-Process -Id ([int]$Entry.id) -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $null
    }
    try {
        $expected = [IO.Path]::GetFullPath($context.VenvPython)
        if ([IO.Path]::GetFullPath($process.Path) -ne $expected) {
            return $null
        }
        if ([long]$Entry.started_ticks -ne $process.StartTime.ToUniversalTime().Ticks) {
            return $null
        }
    } catch {
        return $null
    }
    return $process
}

function Get-ProcessEntries {
    $state = Read-StackState
    if ($null -eq $state) {
        return @()
    }
    return @($state.processes)
}

function Write-StackState {
    param([array]$Entries)
    $temporary = "$statePath.tmp"
    [PSCustomObject]@{
        schema_version = 1
        created_at = [DateTime]::UtcNow.ToString("o")
        processes = $Entries
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $statePath -Force
}

function Start-ManagedProcess {
    param([string]$Name, [string[]]$Arguments)

    $stdout = Join-Path $context.LogDirectory "$Name.out.log"
    $stderr = Join-Path $context.LogDirectory "$Name.err.log"
    $process = Start-Process -FilePath $context.VenvPython `
        -ArgumentList $Arguments `
        -WorkingDirectory $context.ProjectRoot `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -WindowStyle Hidden `
        -PassThru
    Start-Sleep -Milliseconds 400
    if ($process.HasExited) {
        $detail = ""
        if (Test-Path -LiteralPath $stderr) {
            $detail = (Get-Content -LiteralPath $stderr -Tail 40) -join [Environment]::NewLine
        }
        throw "Khong khoi dong duoc $Name. $detail"
    }
    return [PSCustomObject]@{
        name = $Name
        id = $process.Id
        started_ticks = $process.StartTime.ToUniversalTime().Ticks
    }
}

function Stop-OneEntry {
    param($Entry)
    $process = Get-ValidatedProcess -Entry $Entry
    if ($null -eq $process) {
        return
    }
    & taskkill.exe /PID $process.Id /T /F | Out-Null
}

function Stop-Entries {
    param([array]$Entries)
    foreach ($entry in ($Entries | Sort-Object -Property name -Descending)) {
        Stop-OneEntry -Entry $entry
    }
    if (Test-Path -LiteralPath $statePath) {
        Remove-Item -LiteralPath $statePath -Force
    }
}

function Start-Stack {
    Assert-Runtime
    $existing = Get-ProcessEntries
    $running = @($existing | Where-Object { $null -ne (Get-ValidatedProcess -Entry $_) })
    if ($running.Count -gt 0) {
        if ($running.Count -eq 2) {
            Write-Output "Stack Windows dang chay"
            return
        }
        Stop-Entries -Entries $existing
    }

    $started = @()
    try {
        $started += Start-ManagedProcess -Name "worker" -Arguments @("-m", "dub_server.worker")
        $started += Start-ManagedProcess -Name "api" -Arguments @(
            "-m", "uvicorn", "dub_server.api:app",
            "--host", $env:DUB_API_HOST,
            "--port", $env:DUB_API_PORT,
            "--workers", "1"
        )
        Write-StackState -Entries $started
        $healthy = $false
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            try {
                $response = Invoke-WebRequest -UseBasicParsing -Uri "$($env:DUB_API_URL)/v1/health" -TimeoutSec 2
                if ($response.StatusCode -eq 200) {
                    $healthy = $true
                    break
                }
            } catch {
                Start-Sleep -Seconds 1
            }
        }
        if (-not $healthy) {
            throw "API khong healthy sau 30 giay"
        }
        Write-Output "Dashboard: $($env:DUB_API_URL)/"
    } catch {
        Stop-Entries -Entries $started
        throw
    }
}

function Show-Status {
    $entries = Get-ProcessEntries
    if ($entries.Count -eq 0) {
        Write-Output "Stack Windows chua chay"
        exit 1
    }
    $failed = $false
    foreach ($entry in $entries) {
        $process = Get-ValidatedProcess -Entry $entry
        if ($null -eq $process) {
            Write-Output ("{0,-12} STOPPED" -f $entry.name)
            $failed = $true
        } else {
            Write-Output ("{0,-12} RUNNING pid {1}" -f $entry.name, $process.Id)
        }
    }
    if ($failed) {
        exit 1
    }
}

function Show-Logs {
    if ($Lines -lt 1 -or $Lines -gt 100000) {
        throw "Lines phai nam trong khoang 1..100000"
    }
    Get-ChildItem -LiteralPath $context.LogDirectory -Filter "*.log" -ErrorAction SilentlyContinue |
        Sort-Object Name |
        ForEach-Object {
            Write-Output "[$($_.Name)]"
            Get-Content -LiteralPath $_.FullName -Tail $Lines
        }
}

switch ($Action) {
    "start" { Start-Stack }
    "stop" {
        Stop-Entries -Entries (Get-ProcessEntries)
        Write-Output "Stack Windows da dung"
    }
    "restart" {
        Stop-Entries -Entries (Get-ProcessEntries)
        Start-Stack
    }
    "status" { Show-Status }
    "logs" { Show-Logs }
    "foreground" {
        Assert-Runtime
        $running = @(Get-ProcessEntries | Where-Object { $null -ne (Get-ValidatedProcess -Entry $_) })
        if ($running.Count -gt 0) {
            throw "Stack Windows dang chay; hay dung no truoc khi chay foreground"
        }
        $worker = Start-ManagedProcess -Name "worker-foreground" -Arguments @("-m", "dub_server.worker")
        try {
            & $context.VenvPython -m uvicorn dub_server.api:app --host $env:DUB_API_HOST --port $env:DUB_API_PORT --workers 1
            exit $LASTEXITCODE
        } finally {
            Stop-OneEntry -Entry $worker
        }
    }
}
