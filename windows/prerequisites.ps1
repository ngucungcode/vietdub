Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$script:DubPrerequisiteRestartRequired = $false
$script:DubCudaVersion = "12.8"
$script:DubVisualStudioBootstrapUrl = "https://aka.ms/vs/17/release/vs_BuildTools.exe"

function Test-DubAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Assert-DubWindowsPlatform {
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "Can Windows x64"
    }
    $version = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    if ([int]$version.CurrentBuildNumber -lt 19045) {
        throw "Can Windows 10 22H2 x64 build 19045 tro len"
    }
}

function Add-DubPathCandidate {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [System.Collections.Generic.List[string]]$Paths,
        [string]$Path
    )
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return
    }
    $expanded = [Environment]::ExpandEnvironmentVariables($Path.Trim())
    if (-not (Test-Path -LiteralPath $expanded -PathType Container)) {
        return
    }
    foreach ($existing in $Paths) {
        if ($existing.Equals($expanded, [StringComparison]::OrdinalIgnoreCase)) {
            return
        }
    }
    $Paths.Add($expanded)
}

function Update-DubProcessPath {
    $paths = New-Object System.Collections.Generic.List[string]
    $configuredCudaRoot = [Environment]::GetEnvironmentVariable("CUDA_PATH", "Machine")
    if ([string]::IsNullOrWhiteSpace($configuredCudaRoot)) {
        $configuredCudaRoot = $env:CUDA_PATH
    }
    foreach ($candidate in @(
        $(if (-not [string]::IsNullOrWhiteSpace($configuredCudaRoot)) { Join-Path $configuredCudaRoot "bin" }),
        (Join-Path $env:ProgramFiles "NVIDIA GPU Computing Toolkit\CUDA\v12.8\bin"),
        (Join-Path $env:ProgramFiles "NVIDIA GPU Computing Toolkit\CUDA\v12.6\bin"),
        (Join-Path $env:ProgramFiles "Git\cmd"),
        (Join-Path $env:ProgramFiles "CMake\bin"),
        (Join-Path $env:ProgramFiles "Python312"),
        (Join-Path $env:ProgramFiles "Python311"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links"),
        (Join-Path $env:ProgramFiles "WinGet\Links"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps")
    )) {
        Add-DubPathCandidate -Paths $paths -Path $candidate
    }
    foreach ($scope in @("Machine", "User", "Process")) {
        $value = [Environment]::GetEnvironmentVariable("Path", $scope)
        if ([string]::IsNullOrWhiteSpace($value)) {
            continue
        }
        foreach ($candidate in ($value -split ";")) {
            Add-DubPathCandidate -Paths $paths -Path $candidate
        }
    }
    $env:Path = $paths -join ";"

    $cudaRoots = @($configuredCudaRoot)
    $cudaRoots += @(
        (Join-Path $env:ProgramFiles "NVIDIA GPU Computing Toolkit\CUDA\v12.8"),
        (Join-Path $env:ProgramFiles "NVIDIA GPU Computing Toolkit\CUDA\v12.6")
    )
    foreach ($cudaRoot in $cudaRoots) {
        if ([string]::IsNullOrWhiteSpace($cudaRoot)) {
            continue
        }
        $nvcc = Join-Path $cudaRoot "bin\nvcc.exe"
        if (Test-Path -LiteralPath $nvcc -PathType Leaf) {
            $env:CUDA_PATH = $cudaRoot
            $env:CUDACXX = $nvcc
            break
        }
    }
}

function Get-DubCommandPath {
    param([Parameter(Mandatory = $true)][string]$Name)
    Update-DubProcessPath
    $command = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $command) {
        return $null
    }
    return $command.Source
}

function Invoke-DubNativeProbe {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$IncludeStandardError
    )

    try {
        if ($IncludeStandardError) {
            $previousErrorActionPreference = $ErrorActionPreference
            try {
                # Windows PowerShell 5.1 wraps native stderr as ErrorRecord
                # objects. Capture those records without turning an exit-code
                # zero version probe into a terminating NativeCommandError.
                $ErrorActionPreference = "Continue"
                $output = @(& $FilePath @Arguments 2>&1)
                $exitCode = $LASTEXITCODE
            } finally {
                $ErrorActionPreference = $previousErrorActionPreference
            }
            if ($exitCode -ne 0) {
                return $null
            }
            return (($output | ForEach-Object { $_.ToString() }) -join "`n")
        }
        $output = (& $FilePath @Arguments 2>$null | Select-Object -Last 1)
        if ($LASTEXITCODE -ne 0) {
            return $null
        }
        return $output
    } catch {
        # Windows PowerShell 5.1 turns native stderr into an ErrorRecord when
        # ErrorActionPreference is Stop. A failed discovery probe is simply a
        # missing candidate; prerequisite installation handles it later.
        return $null
    }
}

function Get-DubPythonExecutable {
    Update-DubProcessPath
    $candidates = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($env:DUB_WINDOWS_BASE_PYTHON)) {
        $candidates.Add($env:DUB_WINDOWS_BASE_PYTHON)
    }
    $launcher = Get-Command "py.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $launcher) {
        foreach ($selector in @("-3.12", "-3.11")) {
            $resolved = Invoke-DubNativeProbe -FilePath $launcher.Source -Arguments @(
                $selector, "-c", "import sys; print(sys.executable)"
            )
            if (-not [string]::IsNullOrWhiteSpace($resolved)) {
                $candidates.Add($resolved.Trim())
            }
        }
    }
    foreach ($candidate in @(
        (Join-Path $env:ProgramFiles "Python312\python.exe"),
        (Join-Path $env:ProgramFiles "Python311\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe")
    )) {
        $candidates.Add($candidate)
    }
    $pythonCommand = Get-Command "python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $pythonCommand) {
        $candidates.Add($pythonCommand.Source)
    }

    $visited = @{}
    foreach ($candidate in $candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate)) {
            continue
        }
        $candidateKey = $candidate.ToLowerInvariant()
        if ($visited.ContainsKey($candidateKey)) { continue }
        $visited[$candidateKey] = $true
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            continue
        }
        $summary = Invoke-DubNativeProbe -FilePath $candidate -Arguments @(
            "-c", "import struct,sys; print(f'{sys.version_info.major}.{sys.version_info.minor};{struct.calcsize(chr(80))*8}')"
        )
        if ($summary -match '^(3\.11|3\.12);64$') {
            return [IO.Path]::GetFullPath($candidate)
        }
    }
    return $null
}

function Install-DubWinGet {
    $winget = Get-DubCommandPath -Name "winget.exe"
    if (-not [string]::IsNullOrWhiteSpace($winget)) {
        return $winget
    }
    Write-Host "Dang cai/repair Windows Package Manager (WinGet)..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Install-PackageProvider -Name NuGet -Force | Out-Null
    Install-Module -Name Microsoft.WinGet.Client -Force -Repository PSGallery | Out-Null
    Import-Module Microsoft.WinGet.Client -Force
    Repair-WinGetPackageManager -Force -Latest | Out-Null
    Update-DubProcessPath
    $winget = Get-DubCommandPath -Name "winget.exe"
    if ([string]::IsNullOrWhiteSpace($winget)) {
        throw "Khong cai duoc WinGet bang quy trinh repair chinh thuc cua Microsoft"
    }
    return $winget
}

function Invoke-DubWinGetInstall {
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [string]$Version,
        [switch]$Force
    )
    $winget = Install-DubWinGet
    $arguments = @(
        "install", "--id", $Id, "--exact", "--source", "winget",
        "--silent", "--disable-interactivity",
        "--accept-source-agreements", "--accept-package-agreements"
    )
    if (-not [string]::IsNullOrWhiteSpace($Version)) {
        $arguments += @("--version", $Version)
    }
    if ($Force) {
        $arguments += "--force"
    }
    Write-Host "Dang cai prerequisite $Id..."
    & $winget @arguments | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "WinGet khong cai duoc $Id (exit code $LASTEXITCODE)"
    }
    Update-DubProcessPath
}

function Ensure-DubCommandPackage {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string]$PackageId
    )
    if ([string]::IsNullOrWhiteSpace((Get-DubCommandPath -Name $Command))) {
        Invoke-DubWinGetInstall -Id $PackageId
    }
    if ([string]::IsNullOrWhiteSpace((Get-DubCommandPath -Name $Command))) {
        throw "$PackageId da cai nhung van khong tim thay $Command"
    }
}

function Get-DubVisualStudioCppInstallation {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) {
        return $null
    }
    $installation = (& $vswhere -latest -version "[17.0,18.0)" -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath | Select-Object -First 1)
    if ([string]::IsNullOrWhiteSpace($installation)) {
        return $null
    }
    return $installation.Trim()
}

function Assert-DubMicrosoftSignature {
    param([Parameter(Mandatory = $true)][string]$Path)
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
        $null -eq $signature.SignerCertificate -or
        $signature.SignerCertificate.Subject -notmatch '(^|,\s*)O=Microsoft Corporation(,|$)') {
        throw "Chu ky Authenticode Microsoft khong hop le: $Path"
    }
}

function Install-DubVisualStudioCpp {
    if (-not [string]::IsNullOrWhiteSpace((Get-DubVisualStudioCppInstallation))) {
        return
    }
    Write-Host "Dang cai Visual Studio 2022 C++ Build Tools..."
    $bootstrapper = Join-Path $env:TEMP "thuyetminh-vs_BuildTools.exe"
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $script:DubVisualStudioBootstrapUrl -OutFile $bootstrapper
        Assert-DubMicrosoftSignature -Path $bootstrapper
        $process = Start-Process -FilePath $bootstrapper -ArgumentList @(
            "--quiet", "--wait", "--norestart",
            "--add", "Microsoft.VisualStudio.Workload.VCTools",
            "--includeRecommended"
        ) -Wait -PassThru
        if ($process.ExitCode -in @(1641, 3010)) {
            $script:DubPrerequisiteRestartRequired = $true
        } elseif ($process.ExitCode -ne 0) {
            throw "Visual Studio Build Tools that bai voi exit code $($process.ExitCode)"
        }
    } finally {
        if (Test-Path -LiteralPath $bootstrapper -PathType Leaf) {
            Remove-Item -LiteralPath $bootstrapper -Force
        }
    }
    if ([string]::IsNullOrWhiteSpace((Get-DubVisualStudioCppInstallation)) -and
        -not $script:DubPrerequisiteRestartRequired) {
        throw "Da chay Visual Studio installer nhung van thieu workload C++"
    }
}

function Get-DubCudaCompatibility {
    Update-DubProcessPath
    $nvcc = Get-DubCommandPath -Name "nvcc.exe"
    $smi = Get-DubCommandPath -Name "nvidia-smi.exe"
    if ([string]::IsNullOrWhiteSpace($nvcc) -or [string]::IsNullOrWhiteSpace($smi)) {
        return $null
    }
    try {
        $nvccText = (& $nvcc --version 2>&1) -join "`n"
        $match = [Regex]::Match($nvccText, '\brelease\s+([0-9]+\.[0-9]+)\b')
        if (-not $match.Success -or $match.Groups[1].Value -notin @("12.6", "12.8")) {
            return $null
        }
        $release = $match.Groups[1].Value
        $gpuLine = (& $smi --query-gpu=driver_version,compute_cap --format=csv,noheader,nounits | Select-Object -First 1)
        $fields = $gpuLine -split '\s*,\s*'
        if ($fields.Count -ne 2) {
            return $null
        }
        $architecture = [int]$fields[1].Replace(".", "")
        $driverFloors = @{ "12.6" = "560.76"; "12.8" = "570.65" }
        if (([version]$fields[0]) -lt ([version]$driverFloors[$release])) {
            return $null
        }
        if ($architecture -eq 120 -and $release -ne "12.8") {
            return $null
        }
        $architectures = (& $nvcc --list-gpu-arch 2>&1) -join "`n"
        if ($architectures -notmatch ("(?m)^compute_" + $architecture + "$")) {
            return $null
        }
        return [PSCustomObject]@{
            CudaRelease = $release
            Architecture = $architecture
            Nvcc = $nvcc
            NvidiaSmi = $smi
        }
    } catch {
        return $null
    }
}

function Install-DubCuda {
    $compatible = Get-DubCudaCompatibility
    if ($null -ne $compatible) {
        return $compatible
    }
    Write-Host "Dang cai NVIDIA driver va CUDA Toolkit $script:DubCudaVersion..."
    Invoke-DubWinGetInstall -Id "Nvidia.CUDA" -Version $script:DubCudaVersion -Force
    Update-DubProcessPath
    $compatible = Get-DubCudaCompatibility
    if ($null -eq $compatible) {
        $script:DubPrerequisiteRestartRequired = $true
    }
    return $compatible
}

function Get-DubEligibleGpu {
    Update-DubProcessPath
    $smi = Get-DubCommandPath -Name "nvidia-smi.exe"
    if ([string]::IsNullOrWhiteSpace($smi)) {
        return $null
    }
    try {
        $line = (& $smi --query-gpu=uuid,name,driver_version,memory.total,compute_cap --format=csv,noheader,nounits 2>$null | Select-Object -First 1)
        if ([string]::IsNullOrWhiteSpace($line)) {
            return $null
        }
        $fields = $line -split '\s*,\s*'
        if ($fields.Count -ne 5) {
            return $null
        }
        $architecture = [int]$fields[4].Replace(".", "")
        return [PSCustomObject]@{
            Uuid = $fields[0]
            Name = $fields[1]
            Driver = $fields[2]
            MemoryMiB = [int]$fields[3]
            Capability = $fields[4]
            Architecture = $architecture
            Eligible = ($architecture -in @(70, 75, 80, 86, 89, 90, 120) -and [int]$fields[3] -ge 6144)
        }
    } catch {
        return $null
    }
}

function Resolve-DubComputeMode {
    param(
        [ValidateSet("auto", "cpu", "gpu")]
        [string]$Requested = "auto"
    )
    if ($Requested -eq "cpu") {
        return "cpu"
    }
    $gpu = Get-DubEligibleGpu
    if ($Requested -eq "gpu") {
        if ($null -eq $gpu) {
            throw "Khong tim thay NVIDIA GPU de chay GPU mode; dung -ComputeMode cpu"
        }
        if (-not $gpu.Eligible) {
            throw "GPU $($gpu.Name) (sm_$($gpu.Architecture), $($gpu.MemoryMiB) MiB) khong nam trong ma tran release; dung -ComputeMode cpu"
        }
        return "gpu"
    }
    if ($null -ne $gpu -and $gpu.Eligible) {
        return "gpu"
    }
    return "cpu"
}

function Test-DubPrerequisitesReady {
    param(
        [ValidateSet("cpu", "gpu")]
        [string]$ComputeMode = "gpu"
    )
    Update-DubProcessPath
    if ([string]::IsNullOrWhiteSpace((Get-DubPythonExecutable))) {
        return $false
    }
    foreach ($command in @("git.exe", "cmake.exe", "ninja.exe", "ffmpeg.exe", "ffprobe.exe")) {
        if ([string]::IsNullOrWhiteSpace((Get-DubCommandPath -Name $command))) {
            return $false
        }
    }
    if ([string]::IsNullOrWhiteSpace((Get-DubVisualStudioCppInstallation))) {
        return $false
    }
    if ($ComputeMode -eq "cpu") {
        return $true
    }
    return $null -ne (Get-DubCudaCompatibility)
}

function Install-DubPrerequisites {
    param(
        [ValidateSet("cpu", "gpu")]
        [string]$ComputeMode = "gpu"
    )
    Assert-DubWindowsPlatform
    if (-not (Test-DubAdministrator)) {
        throw "Can quyen Administrator de tu dong cai prerequisite"
    }

    [void](Install-DubWinGet)
    if ([string]::IsNullOrWhiteSpace((Get-DubPythonExecutable))) {
        Invoke-DubWinGetInstall -Id "Python.Python.3.12"
    }
    $python = Get-DubPythonExecutable
    if ([string]::IsNullOrWhiteSpace($python)) {
        throw "Da cai Python.Python.3.12 nhung khong tim thay Python 3.11/3.12 x64"
    }
    $env:DUB_WINDOWS_BASE_PYTHON = $python

    Ensure-DubCommandPackage -Command "git.exe" -PackageId "Git.Git"
    Ensure-DubCommandPackage -Command "cmake.exe" -PackageId "Kitware.CMake"
    Ensure-DubCommandPackage -Command "ninja.exe" -PackageId "Ninja-build.Ninja"
    Ensure-DubCommandPackage -Command "ffmpeg.exe" -PackageId "Gyan.FFmpeg"
    if ([string]::IsNullOrWhiteSpace((Get-DubCommandPath -Name "ffprobe.exe"))) {
        throw "Gyan.FFmpeg da cai nhung khong tim thay ffprobe.exe"
    }
    Install-DubVisualStudioCpp
    $cuda = $null
    if ($ComputeMode -eq "gpu") {
        $cuda = Install-DubCuda
    } else {
        Write-Host "CPU mode: bo qua NVIDIA driver va CUDA Toolkit."
    }
    Update-DubProcessPath

    return [PSCustomObject]@{
        Python = $python
        Cuda = $cuda
        RestartRequired = $script:DubPrerequisiteRestartRequired
    }
}
