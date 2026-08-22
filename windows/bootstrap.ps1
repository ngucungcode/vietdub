[CmdletBinding()]
param(
    [ValidateSet("auto", "minimal", "balanced", "maximum")]
    [string]$Profile = "auto",
    [ValidateSet("auto", "cpu", "gpu")]
    [string]$ComputeMode = "auto",
    [switch]$SkipModels,
    [switch]$SkipStart,
    [switch]$NoOpenDashboard,
    [switch]$FullTest,
    [ValidateRange(1, 32)]
    [int]$BuildJobs = 4,
    [ValidatePattern('^[A-Za-z0-9._/-]+$')]
    [string]$SourceRef = "main",
    [string]$InstallRoot = ""
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT" -or -not [Environment]::Is64BitOperatingSystem) {
    throw "Bootstrap nay chi chay tren Windows x64"
}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Get-BundledProjectRoot {
    $candidate = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
    if ((Test-Path -LiteralPath (Join-Path $candidate "windows\install.ps1") -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $candidate "native\components.lock.json") -PathType Leaf)) {
        return $candidate
    }
    return $null
}

function Copy-DubDirectoryContents {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    [void](New-Item -ItemType Directory -Force -Path $Destination)
    foreach ($item in (Get-ChildItem -LiteralPath $Source -Force)) {
        $target = Join-Path $Destination $item.Name
        if ($item.PSIsContainer) {
            Copy-DubDirectoryContents -Source $item.FullName -Destination $target
        } else {
            Copy-Item -LiteralPath $item.FullName -Destination $target -Force
        }
    }
}

function Install-DubProjectSource {
    param([Parameter(Mandatory = $true)][string]$Destination)

    $destinationExists = Test-Path -LiteralPath $Destination
    if ($destinationExists) {
        $existingInstaller = Join-Path $Destination "windows\install.ps1"
        $existingManifest = Join-Path $Destination "native\components.lock.json"
        if (-not (Test-Path -LiteralPath $Destination -PathType Container) -or
            -not (Test-Path -LiteralPath $existingInstaller -PathType Leaf) -or
            -not (Test-Path -LiteralPath $existingManifest -PathType Leaf)) {
            throw "Thu muc cai dat da ton tai nhung khong phai source hop le: $Destination"
        }
    }

    $temporaryRoot = Join-Path $env:TEMP ("thuyetminh-bootstrap-" + [Guid]::NewGuid().ToString("N"))
    $archive = Join-Path $temporaryRoot "source.zip"
    $expanded = Join-Path $temporaryRoot "expanded"
    try {
        [void](New-Item -ItemType Directory -Force -Path $expanded)
        $archiveUrl = "https://github.com/ngucungcode/vietdub/archive/refs/heads/$SourceRef.zip"
        # Keep progress messages off the success stream. The caller captures this
        # function's success output as the project path, so Write-Output here would
        # turn $projectRoot into an array and make PowerShell try to execute this
        # message instead of install.ps1.
        Write-Host "Dang tai source $SourceRef tu GitHub..."
        Invoke-WebRequest -UseBasicParsing -Uri $archiveUrl -OutFile $archive
        Expand-Archive -LiteralPath $archive -DestinationPath $expanded
        $sourceRoot = Get-ChildItem -LiteralPath $expanded -Directory |
            Where-Object {
                (Test-Path -LiteralPath (Join-Path $_.FullName "windows\install.ps1") -PathType Leaf) -and
                (Test-Path -LiteralPath (Join-Path $_.FullName "native\components.lock.json") -PathType Leaf)
            } |
            Select-Object -First 1
        if ($null -eq $sourceRoot) {
            throw "Archive GitHub khong chua source Windows hop le"
        }
        $parent = Split-Path -Parent $Destination
        [void](New-Item -ItemType Directory -Force -Path $parent)
        if ($destinationExists) {
            Write-Host "Dang cap nhat source $SourceRef da tai truoc do..."
            Copy-DubDirectoryContents -Source $sourceRoot.FullName -Destination $Destination
        } else {
            Move-Item -LiteralPath $sourceRoot.FullName -Destination $Destination
        }
        return [IO.Path]::GetFullPath($Destination)
    } finally {
        if (Test-Path -LiteralPath $temporaryRoot -PathType Container) {
            Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
        }
    }
}

$projectRoot = Get-BundledProjectRoot
if ([string]::IsNullOrWhiteSpace($projectRoot)) {
    if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
        if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
            throw "Khong doc duoc LOCALAPPDATA de chon thu muc cai dat"
        }
        $InstallRoot = Join-Path $env:LOCALAPPDATA "Programs\ThuyetMinhOfflineGPU\source"
    }
    if (-not [IO.Path]::IsPathRooted($InstallRoot)) {
        throw "InstallRoot phai la duong dan Windows tuyet doi"
    }
    $resolvedProjectRoots = @(Install-DubProjectSource -Destination ([IO.Path]::GetFullPath($InstallRoot)))
    if ($resolvedProjectRoots.Count -ne 1) {
        throw "Bootstrap source resolver tra ve du lieu khong hop le"
    }
    $projectRoot = [string]$resolvedProjectRoots[0]
}

$installer = Join-Path $projectRoot "windows\install.ps1"
$installerArguments = @{
    Profile = $Profile
    ComputeMode = $ComputeMode
    BuildJobs = $BuildJobs
}
if ($SkipModels) { $installerArguments.SkipModels = $true }
if ($SkipStart) { $installerArguments.SkipStart = $true }
if (-not $NoOpenDashboard -and -not $SkipStart) { $installerArguments.OpenDashboard = $true }
if ($FullTest) { $installerArguments.FullTest = $true }

Write-Output "Dang chay bo cai Windows native tu $projectRoot"
& $installer @installerArguments
