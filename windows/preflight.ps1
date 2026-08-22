[CmdletBinding()]
param(
    [switch]$RequireRuntime,
    [ValidateSet("auto", "cpu", "gpu")]
    [string]$ComputeMode = "auto"
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
. (Join-Path $PSScriptRoot "prerequisites.ps1")

$context = Initialize-DubWindowsEnvironment
$selectedComputeMode = $ComputeMode
if ($selectedComputeMode -eq "auto") {
    if ($env:DUB_COMPUTE_MODE -in @("cpu", "gpu")) {
        $selectedComputeMode = $env:DUB_COMPUTE_MODE
    } else {
        $selectedComputeMode = Resolve-DubComputeMode -Requested "auto"
    }
}
$checks = New-Object System.Collections.Generic.List[object]
Update-DubProcessPath

function Add-Check {
    param([string]$Name, [string]$Status, [string]$Message)
    $checks.Add([PSCustomObject]@{
        name = $Name
        status = $Status
        message = $Message
    })
}

if (-not [Environment]::Is64BitOperatingSystem) {
    Add-Check "windows" "error" "Can Windows x64"
} else {
    $version = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    $build = [int]$version.CurrentBuildNumber
    $display = $version.DisplayVersion
    if ($build -lt 19045) {
        Add-Check "windows" "error" "Can Windows 10 22H2 build 19045 tro len; hien tai $display build $build"
    } else {
        Add-Check "windows" "ok" "$display build $build x64"
    }
}

$pythonExecutable = Get-DubPythonExecutable
if ([string]::IsNullOrWhiteSpace($pythonExecutable)) {
    Add-Check "python" "error" "Khong tim thay Python 3.11/3.12 x64"
} else {
    Add-Check "python" "ok" $pythonExecutable
}

$requiredCommands = @("git.exe", "cmake.exe", "ninja.exe", "ffmpeg.exe", "ffprobe.exe")
if ($selectedComputeMode -eq "gpu") {
    $requiredCommands += @("nvidia-smi.exe", "nvcc.exe")
}
foreach ($commandName in $requiredCommands) {
    $command = Get-Command $commandName -ErrorAction SilentlyContinue
    $checkName = $commandName.Replace(".exe", "")
    if ($null -eq $command) {
        Add-Check $checkName "error" "Khong tim thay trong PATH"
    } else {
        Add-Check $checkName "ok" $command.Source
    }
}

$gpuFields = $null
$gpuArchitecture = $null
try {
    if ([string]::IsNullOrWhiteSpace($pythonExecutable)) {
        throw "Can Python 3.11/3.12 x64"
    }
    $pythonSummary = (& $pythonExecutable -c "import struct,sys; print(f'{sys.version_info.major}.{sys.version_info.minor};{struct.calcsize(chr(80))*8}')").Trim()
    $pythonFields = $pythonSummary -split ";"
    if ($pythonFields.Count -ne 2 -or $pythonFields[0] -notin @("3.11", "3.12") -or $pythonFields[1] -ne "64") {
        throw "Can Python 3.11/3.12 x64; hien tai $pythonSummary"
    }
    Add-Check "python-version" "ok" "Python $($pythonFields[0]) x64"
} catch {
    Add-Check "python-version" "error" $_.Exception.Message
}

try {
    $ramBytes = Get-DubInstalledMemoryBytes
    if ($ramBytes -lt 16GB) {
        throw "Can it nhat 16 GiB RAM vat ly; hien tai $([Math]::Round($ramBytes / 1GB, 1)) GiB"
    }
    Add-Check "ram" "ok" "$([Math]::Round($ramBytes / 1GB, 1)) GiB"
} catch {
    Add-Check "ram" "error" $_.Exception.Message
}

$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) {
    Add-Check "visual-studio" "error" "Khong tim thay Visual Studio Installer/vswhere.exe"
} else {
    $vsInstall = (& $vswhere -latest -version "[17.0,18.0)" -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath | Select-Object -First 1)
    if ([string]::IsNullOrWhiteSpace($vsInstall)) {
        Add-Check "visual-studio" "error" "Can Visual Studio 2022 Build Tools voi Desktop development with C++"
    } else {
        Add-Check "visual-studio" "ok" $vsInstall
    }
}

if ($selectedComputeMode -eq "gpu") {
try {
    $gpuLine = (& nvidia-smi --query-gpu=uuid,name,driver_version,memory.total,compute_cap --format=csv,noheader,nounits | Select-Object -First 1)
    if ([string]::IsNullOrWhiteSpace($gpuLine)) {
        throw "nvidia-smi khong tra GPU"
    }
    $gpuFields = $gpuLine -split '\s*,\s*'
    if ($gpuFields.Count -ne 5) {
        throw "nvidia-smi tra du lieu khong hop le"
    }
    $gpuArchitecture = [int]$gpuFields[4].Replace(".", "")
    if ($gpuArchitecture -notin @(70, 75, 80, 86, 89, 90, 120)) {
        throw "GPU $($gpuFields[1]) sm_$gpuArchitecture khong nam trong ma tran release"
    }
    if ([int]$gpuFields[3] -lt 6144) {
        throw "GPU chi co $($gpuFields[3]) MiB; can it nhat 6144 MiB"
    }
    Add-Check "gpu" "ok" ("{0}; sm_{1}; {2} MiB; driver {3}" -f $gpuFields[1], $gpuArchitecture, $gpuFields[3], $gpuFields[2])

} catch {
    Add-Check "gpu" "error" $_.Exception.Message
    $gpuFields = $null
    $gpuArchitecture = $null
}

if ($null -ne $gpuFields -and $null -ne $gpuArchitecture) {
    try {
        $nvccCommand = Get-Command nvcc -ErrorAction SilentlyContinue
        if ($null -eq $nvccCommand) {
            throw "Khong tim thay nvcc trong PATH"
        }
        $nvccText = (& nvcc --version) -join "`n"
        $nvccMatch = [Regex]::Match($nvccText, '\brelease\s+([0-9]+\.[0-9]+)\b')
        if (-not $nvccMatch.Success) {
            throw "nvcc khong bao CUDA release"
        }
        $cudaRelease = $nvccMatch.Groups[1].Value
        $driverFloors = @{ "12.6" = "560.76"; "12.8" = "570.65" }
        if (-not $driverFloors.ContainsKey($cudaRelease)) {
            throw "CUDA $cudaRelease khong nam trong ma tran Windows 12.6/12.8"
        }
        if (([version]$gpuFields[2]) -lt ([version]$driverFloors[$cudaRelease])) {
            throw "Driver $($gpuFields[2]) thap hon $($driverFloors[$cudaRelease]) cho CUDA $cudaRelease"
        }
        if ($gpuArchitecture -eq 120 -and $cudaRelease -ne "12.8") {
            throw "RTX 50 sm_120 can CUDA 12.8"
        }
        $nvccArchitectures = (& nvcc --list-gpu-arch) -join "`n"
        if ($nvccArchitectures -notmatch ("(?m)^compute_" + $gpuArchitecture + "$")) {
            throw "CUDA $cudaRelease khong build duoc sm_$gpuArchitecture"
        }
        Add-Check "cuda-compat" "ok" "CUDA $cudaRelease; driver floor $($driverFloors[$cudaRelease]); compute_$gpuArchitecture"
    } catch {
        Add-Check "cuda-compat" "error" $_.Exception.Message
    }
}
} else {
    Add-Check "compute" "ok" "CPU compatibility mode; CUDA khong bat buoc"
}

if ($RequireRuntime) {
    if (Test-Path -LiteralPath $context.VenvPython -PathType Leaf) {
        Add-Check "venv" "ok" $context.VenvPython
        try {
            if ($selectedComputeMode -eq "gpu") {
                $probeScript = "from dub_server.gpu import inspect_gpu; r=inspect_gpu(require_gpu=True); print(r.gpus[0].name)"
                $probeName = "python-gpu"
            } else {
                $probeScript = "import torch,ctranslate2; assert not torch.cuda.is_available() and 'int8' in ctranslate2.get_supported_compute_types('cpu'); print('CPU int8')"
                $probeName = "python-cpu"
            }
            $probe = & $context.VenvPython -c $probeScript 2>&1
            if ($LASTEXITCODE -ne 0) {
                throw ($probe -join [Environment]::NewLine)
            }
            Add-Check $probeName "ok" ($probe | Select-Object -Last 1)
        } catch {
            Add-Check "python-runtime" "error" $_.Exception.Message
        }
    } else {
        Add-Check "venv" "error" "Chua chay windows\install.ps1"
    }
    foreach ($runtimePath in @($env:DUB_LLAMA_SERVER_BINARY, $env:DUB_TIGER_SOURCE_DIR, $env:DUB_VIENEU_ENTRYPOINT)) {
        if (Test-Path -LiteralPath $runtimePath) {
            Add-Check "runtime" "ok" $runtimePath
        } else {
            Add-Check "runtime" "error" "Thieu $runtimePath"
        }
    }
}

$overall = "ok"
if ($checks | Where-Object { $_.status -eq "error" }) {
    $overall = "error"
} elseif ($checks | Where-Object { $_.status -eq "warning" }) {
    $overall = "warning"
}

[PSCustomObject]@{
    status = $overall
    checks = $checks
} | ConvertTo-Json -Depth 5

if ($overall -eq "error") {
    exit 1
}
