[CmdletBinding()]
param(
    [ValidateSet("auto", "minimal", "balanced", "maximum")]
    [string]$Profile = "auto",
    [ValidateSet("auto", "cpu", "gpu")]
    [string]$ComputeMode = "auto",
    [switch]$SkipModels,
    [switch]$SkipPrerequisites,
    [switch]$SkipStart,
    [switch]$OpenDashboard,
    [switch]$FullTest,
    [ValidateRange(1, 32)]
    [int]$BuildJobs = 4,
    [switch]$PrerequisitesOnly,
    [switch]$Elevated
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
. (Join-Path $PSScriptRoot "prerequisites.ps1")
$selectedComputeMode = Resolve-DubComputeMode -Requested $ComputeMode

function Invoke-DubElevatedPrerequisites {
    $windowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    if (-not (Test-Path -LiteralPath $windowsPowerShell -PathType Leaf)) {
        throw "Khong tim thay Windows PowerShell de nang quyen UAC"
    }
    $arguments = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        ('"{0}"' -f $PSCommandPath),
        "-PrerequisitesOnly",
        "-Elevated",
        "-ComputeMode", $selectedComputeMode
    )

    Write-Output "Dang yeu cau quyen Administrator qua UAC de cai prerequisite..."
    $process = Start-Process -FilePath $windowsPowerShell `
        -ArgumentList $arguments `
        -Verb RunAs `
        -Wait `
        -PassThru
    if ($process.ExitCode -eq 3010) {
        throw "Prerequisite da cai xong nhung Windows can reboot. Khoi dong lai may roi chay lai dung lenh install.ps1"
    }
    if ($process.ExitCode -ne 0) {
        throw "Tien trinh cai prerequisite that bai voi exit code $($process.ExitCode)"
    }
}

$prerequisitesInstalledByChild = $false
if ($PrerequisitesOnly) {
    if (-not $Elevated -or -not (Test-DubAdministrator)) {
        throw "PrerequisitesOnly chi duoc goi boi tien trinh UAC cua installer"
    }
    $result = Install-DubPrerequisites -ComputeMode $selectedComputeMode
    if ($result.RestartRequired) {
        exit 3010
    }
    exit 0
}
if (-not $SkipPrerequisites -and -not (Test-DubAdministrator)) {
    if (Test-DubPrerequisitesReady -ComputeMode $selectedComputeMode) {
        $prerequisitesInstalledByChild = $true
    } else {
        if ($Elevated) {
            throw "Khong nhan duoc quyen Administrator sau khi mo UAC"
        }
        Invoke-DubElevatedPrerequisites
        $prerequisitesInstalledByChild = $true
    }
}

function Require-Command {
    param([Parameter(Mandatory = $true)][string]$Name)
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        throw "Thieu $Name trong PATH. Xem docs\windows-native.md"
    }
    return $command.Source
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath that bai voi exit code $LASTEXITCODE"
    }
}

function Set-DubEnvValue {
    param([string]$Path, [string]$Name, [string]$Value)
    if ($Value.Contains("`r") -or $Value.Contains("`n")) {
        throw "Gia tri $Name khong hop le"
    }
    $lines = New-Object System.Collections.Generic.List[string]
    $replaced = $false
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ($line -match ("^\s*" + [Regex]::Escape($Name) + "=")) {
            $lines.Add("$Name=$Value")
            $replaced = $true
        } else {
            $lines.Add($line)
        }
    }
    if (-not $replaced) {
        $lines.Add("$Name=$Value")
    }
    [IO.File]::WriteAllLines($Path, $lines, (New-Object Text.UTF8Encoding($false)))
    [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
}

function Import-VisualStudioEnvironment {
    if ($null -ne (Get-Command "cl.exe" -ErrorAction SilentlyContinue)) {
        return
    }
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) {
        throw "Can Visual Studio 2022 Build Tools voi workload Desktop development with C++"
    }
    $installation = (& $vswhere -latest -version "[17.0,18.0)" -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath | Select-Object -First 1)
    if ([string]::IsNullOrWhiteSpace($installation)) {
        throw "Khong tim thay Visual Studio C++ Build Tools"
    }
    $devCommand = Join-Path $installation "Common7\Tools\VsDevCmd.bat"
    if (-not (Test-Path -LiteralPath $devCommand -PathType Leaf)) {
        throw "Khong tim thay VsDevCmd.bat"
    }
    $environmentLines = & cmd.exe /s /c "`"$devCommand`" -no_logo -arch=x64 -host_arch=x64 && set"
    if ($LASTEXITCODE -ne 0) {
        throw "Khong nap duoc Visual Studio build environment"
    }
    foreach ($line in $environmentLines) {
        $separator = $line.IndexOf("=")
        if ($separator -gt 0) {
            [Environment]::SetEnvironmentVariable(
                $line.Substring(0, $separator),
                $line.Substring($separator + 1),
                "Process"
            )
        }
    }
    [void](Require-Command "cl.exe")
}

function Sync-LockedRepository {
    param(
        [string]$Repository,
        [string]$Commit,
        [string]$Destination
    )
    if ($Commit -notmatch '^[0-9a-f]{40}$') {
        throw "Commit lock khong hop le: $Commit"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Destination ".git") -PathType Container)) {
        if (Test-Path -LiteralPath $Destination) {
            throw "Thu muc source da ton tai nhung khong phai Git checkout: $Destination"
        }
        [void](New-Item -ItemType Directory -Force -Path $Destination)
        Invoke-Checked "git.exe" "-C" $Destination "init" "-q"
        Invoke-Checked "git.exe" "-C" $Destination "remote" "add" "origin" $Repository
    }
    Invoke-Checked "git.exe" "-C" $Destination "fetch" "--depth" "1" "origin" $Commit
    Invoke-Checked "git.exe" "-C" $Destination "checkout" "-q" "--detach" "FETCH_HEAD"
    $actual = (& git.exe -C $Destination rev-parse HEAD).Trim()
    if ($actual -ne $Commit) {
        throw "Source tai $Destination khong khop commit lock"
    }
}

function Get-GpuBinding {
    $line = (& nvidia-smi.exe --query-gpu=uuid,name,driver_version,memory.total,compute_cap --format=csv,noheader,nounits | Select-Object -First 1)
    if ([string]::IsNullOrWhiteSpace($line)) {
        throw "nvidia-smi khong tra GPU"
    }
    $fields = $line -split '\s*,\s*'
    if ($fields.Count -ne 5) {
        throw "nvidia-smi tra du lieu khong hop le"
    }
    return [PSCustomObject]@{
        Uuid = $fields[0]
        Name = $fields[1]
        Driver = $fields[2]
        MemoryMiB = [int]$fields[3]
        Capability = $fields[4]
        Architecture = [int]$fields[4].Replace(".", "")
    }
}

function Select-ModelProfile {
    param($Gpu, [string]$Requested)
    if ($Requested -ne "auto") {
        return $Requested
    }
    if ($Gpu.Capability -eq "12.0") {
        return "minimal"
    }
    if ($Gpu.MemoryMiB -ge 22528) {
        return "maximum"
    }
    if ($Gpu.MemoryMiB -ge 8192) {
        return "balanced"
    }
    return "minimal"
}

function Set-ProfileDefaults {
    param([string]$SelectedProfile, [string]$EnvFile)
    $defaults = @{
        minimal = @(
            @("DUB_DEFAULT_ASR_MODEL_ID", "asr-faster-whisper-small"),
            @("DUB_DEFAULT_TRANSLATION_MODEL_ID", "mt-gemma4-e2b-q4"),
            @("DUB_DEFAULT_SEPARATION_MODEL_ID", "separation-tiger-dnr"),
            @("DUB_DEFAULT_TTS_MODEL_ID", "tts-piper-vi-vais1000-medium")
        )
        balanced = @(
            @("DUB_DEFAULT_ASR_MODEL_ID", "asr-faster-whisper-small"),
            @("DUB_DEFAULT_TRANSLATION_MODEL_ID", "mt-gemma4-e2b-q4"),
            @("DUB_DEFAULT_SEPARATION_MODEL_ID", "separation-tiger-dnr"),
            @("DUB_DEFAULT_TTS_MODEL_ID", "tts-vieneu-v2")
        )
        maximum = @(
            @("DUB_DEFAULT_ASR_MODEL_ID", "asr-faster-whisper-large-v3-turbo"),
            @("DUB_DEFAULT_TRANSLATION_MODEL_ID", "mt-gemma4-31b-q4"),
            @("DUB_DEFAULT_SEPARATION_MODEL_ID", "separation-tiger-dnr"),
            @("DUB_DEFAULT_TTS_MODEL_ID", "tts-vieneu-v2")
        )
    }
    foreach ($pair in $defaults[$SelectedProfile]) {
        Set-DubEnvValue -Path $EnvFile -Name $pair[0] -Value $pair[1]
    }
}

function Install-LlamaCpp {
    param(
        $Context,
        $Manifest,
        $Gpu,
        [ValidateSet("cpu", "gpu")][string]$ComputeMode,
        [string]$EnvFile,
        [int]$Jobs
    )
    Import-VisualStudioEnvironment
    $llama = $Manifest.components.llama_cpp
    $cudaVersion = $null
    if ($ComputeMode -eq "gpu") {
        $supportedVersions = @($llama.cuda_supported_versions)
        $nvccOutput = (& nvcc.exe --version) -join "`n"
        $match = [Regex]::Match($nvccOutput, '\brelease\s+([0-9]+\.[0-9]+)\b')
        if (-not $match.Success) {
            throw "nvcc khong bao CUDA release"
        }
        $cudaVersion = $match.Groups[1].Value
        if ($supportedVersions -notcontains $cudaVersion) {
            throw "CUDA $cudaVersion khong nam trong ma tran $($supportedVersions -join ', ')"
        }
        $supportedArchitectures = @($llama.cuda_supported_architectures | ForEach-Object { [int]$_ })
        if ($supportedArchitectures -notcontains $Gpu.Architecture) {
            throw "GPU $($Gpu.Name) sm_$($Gpu.Architecture) chua duoc release nay ho tro"
        }
        $nvccArchitectures = (& nvcc.exe --list-gpu-arch) -join "`n"
        if ($nvccArchitectures -notmatch ("(?m)^compute_" + $Gpu.Architecture + "$")) {
            throw "CUDA toolkit khong build duoc sm_$($Gpu.Architecture)"
        }
    }

    $source = Join-Path $Context.NativeRoot ("cache\llama.cpp-" + $llama.commit)
    Sync-LockedRepository -Repository $llama.repository -Commit $llama.commit -Destination $source
    $shortCommit = $llama.commit.Substring(0, 7)
    if ($ComputeMode -eq "gpu") {
        $runtimeTag = "cuda" + $cudaVersion.Replace(".", "") + "-sm" + $Gpu.Architecture
    } else {
        $runtimeTag = "cpu"
    }
    $build = Join-Path $source ("build-windows-" + $runtimeTag)
    $target = Join-Path $Context.NativeRoot ("opt\llama.cpp-" + $shortCommit + "-" + $runtimeTag)
    $server = Join-Path $target "llama-server.exe"
    $receipt = Join-Path $target "build-receipt.json"
    if ((Test-Path -LiteralPath $server -PathType Leaf) -and (Test-Path -LiteralPath $receipt -PathType Leaf)) {
        $saved = Get-Content -LiteralPath $receipt -Raw | ConvertFrom-Json
        if ($saved.commit -eq $llama.commit -and
            $saved.compute_mode -eq $ComputeMode -and
            ($ComputeMode -eq "cpu" -or
                ($saved.cuda_version -eq $cudaVersion -and
                 [int]$saved.cuda_architecture -eq $Gpu.Architecture))) {
            Set-DubEnvValue -Path $EnvFile -Name "DUB_LLAMA_SERVER_BINARY" -Value $server
            return
        }
        throw "llama.cpp target da ton tai nhung receipt khong khop: $target"
    }
    if (Test-Path -LiteralPath $target) {
        Write-Output "Dang khoi phuc llama.cpp target chua hoan tat: $target"
        Remove-Item -LiteralPath $target -Recurse -Force
    }

    $configureArguments = @(
        "-S"
        $source
        "-B"
        $build
        "-G"
        "Ninja"
        "-DCMAKE_BUILD_TYPE=Release"
        "-DLLAMA_CURL=OFF"
        "-DLLAMA_BUILD_UI=OFF"
        "-DLLAMA_USE_PREBUILT_UI=OFF"
        "-DLLAMA_BUILD_TESTS=OFF"
        "-DLLAMA_BUILD_EXAMPLES=ON"
    )
    if ($ComputeMode -eq "gpu") {
        $configureArguments += @(
            ("-DCMAKE_CUDA_COMPILER=" + $env:CUDACXX),
            ("-DCMAKE_CUDA_ARCHITECTURES=" + $Gpu.Architecture),
            "-DGGML_CUDA=ON"
        )
    } else {
        $configureArguments += "-DGGML_CUDA=OFF"
    }
    Invoke-Checked "cmake.exe" @configureArguments
    Invoke-Checked "cmake.exe" "--build" $build "--target" "llama-server" "llama-cli" "--parallel" $Jobs
    [void](New-Item -ItemType Directory -Force -Path $target)
    Copy-Item -Path (Join-Path $build "bin\*") -Destination $target -Recurse -Force
    if (-not (Test-Path -LiteralPath $server -PathType Leaf)) {
        throw "Build llama.cpp khong tao llama-server.exe"
    }
    $versionOutput = Invoke-DubNativeProbe -FilePath $server -Arguments @("--version") -IncludeStandardError
    if ([string]::IsNullOrWhiteSpace($versionOutput) -or
        $versionOutput -notmatch $llama.commit.Substring(0, 7)) {
        throw "llama-server.exe khong khop commit lock"
    }
    $receiptPayload = [ordered]@{
        schema_version = 1
        release = $llama.release
        commit = $llama.commit
        compute_mode = $ComputeMode
        llama_server_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $server).Hash.ToLowerInvariant()
    }
    if ($ComputeMode -eq "gpu") {
        $receiptPayload["cuda_version"] = $cudaVersion
        $receiptPayload["cuda_architecture"] = $Gpu.Architecture
    }
    [PSCustomObject]$receiptPayload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $receipt -Encoding UTF8
    Set-DubEnvValue -Path $EnvFile -Name "DUB_LLAMA_SERVER_BINARY" -Value $server
}

$projectRoot = Get-DubProjectRoot
$envFile = Join-Path $projectRoot ".env.windows"
if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
    Copy-Item -LiteralPath (Join-Path $projectRoot ".env.windows.example") -Destination $envFile
}
$env:DUB_WINDOWS_ENV_FILE = $envFile
$context = Initialize-DubWindowsEnvironment
New-DubWindowsDirectories -Context $context

Assert-DubWindowsPlatform
$prerequisiteResult = $null
if (-not $SkipPrerequisites -and
    -not $prerequisitesInstalledByChild -and
    -not (Test-DubPrerequisitesReady -ComputeMode $selectedComputeMode)) {
    $prerequisiteResult = Install-DubPrerequisites -ComputeMode $selectedComputeMode
    if ($prerequisiteResult.RestartRequired) {
        throw "Prerequisite da cai xong nhung Windows can reboot. Khoi dong lai may roi chay lai dung lenh install.ps1; installer se tiep tuc idempotent"
    }
}

Update-DubProcessPath
$pythonExecutable = Get-DubPythonExecutable
if ([string]::IsNullOrWhiteSpace($pythonExecutable)) {
    throw "Can Python 3.11 hoac 3.12 x64; dung -SkipPrerequisites chi khi da cai san day du"
}
$env:DUB_WINDOWS_BASE_PYTHON = $pythonExecutable
foreach ($required in @("git.exe", "cmake.exe", "ninja.exe", "ffmpeg.exe", "ffprobe.exe")) {
    [void](Require-Command $required)
}
if ($selectedComputeMode -eq "gpu") {
    [void](Require-Command "nvidia-smi.exe")
    if ([string]::IsNullOrWhiteSpace($env:CUDACXX)) {
        $env:CUDACXX = Require-Command "nvcc.exe"
    }
}

$pythonVersion = (& $pythonExecutable -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ($pythonVersion -notin @("3.11", "3.12", "3.13")) {
    throw "Can Python 3.11, 3.12 hoac 3.13 x64; hien tai $pythonVersion"
}
$pythonBits = (& $pythonExecutable -c "import struct; print(struct.calcsize('P') * 8)").Trim()
if ($pythonBits -ne "64") {
    throw "Can Python x64; hien tai Python $pythonBits-bit"
}
$ramBytes = Get-DubInstalledMemoryBytes
if ($ramBytes -lt 16GB) {
    throw "Can it nhat 16 GiB RAM vat ly; hien tai $([Math]::Round($ramBytes / 1GB, 1)) GiB"
}

$manifest = Get-Content -LiteralPath (Join-Path $projectRoot "native\components.lock.json") -Raw | ConvertFrom-Json
if ($manifest.schema_version -ne 1) {
    throw "native/components.lock.json khong hop le"
}
$gpu = $null
$cudaRelease = $null
if ($selectedComputeMode -eq "gpu") {
    $gpu = Get-GpuBinding
    if ($gpu.MemoryMiB -lt 6144) {
        throw "GPU $($gpu.Name) chi co $($gpu.MemoryMiB) MiB; can it nhat 6144 MiB"
    }
    $supportedArchitectures = @($manifest.components.llama_cpp.cuda_supported_architectures | ForEach-Object { [int]$_ })
    if ($supportedArchitectures -notcontains $gpu.Architecture) {
        throw "GPU $($gpu.Name) sm_$($gpu.Architecture) khong nam trong ma tran release"
    }
    $nvccText = (& nvcc.exe --version) -join "`n"
    $nvccMatch = [Regex]::Match($nvccText, '\brelease\s+([0-9]+\.[0-9]+)\b')
    if (-not $nvccMatch.Success) {
        throw "nvcc khong bao CUDA release"
    }
    $cudaRelease = $nvccMatch.Groups[1].Value
    $windowsDriverFloors = @{ "12.6" = "560.76"; "12.8" = "570.65" }
    if (-not $windowsDriverFloors.ContainsKey($cudaRelease)) {
        throw "CUDA $cudaRelease khong nam trong ma tran Windows 12.6/12.8"
    }
    if (([version]$gpu.Driver) -lt ([version]$windowsDriverFloors[$cudaRelease])) {
        throw "Driver $($gpu.Driver) thap hon $($windowsDriverFloors[$cudaRelease]) cho CUDA $cudaRelease tren Windows"
    }
    if ($gpu.Capability -eq "12.0" -and $cudaRelease -ne "12.8") {
        throw "RTX 50 sm_120 can CUDA 12.8"
    }
    $selectedProfile = Select-ModelProfile -Gpu $gpu -Requested $Profile
    $profileFloors = @{ minimal = 6144; balanced = 8192; maximum = 22528 }
    if ($gpu.Capability -eq "12.0" -and $selectedProfile -ne "minimal") {
        throw "RTX 50 sm_120 dang o tier thu nghiem; chi ho tro profile minimal"
    }
    if ($gpu.MemoryMiB -lt $profileFloors[$selectedProfile]) {
        throw "Profile $selectedProfile can $($profileFloors[$selectedProfile]) MiB; GPU chi co $($gpu.MemoryMiB) MiB"
    }
} else {
    if ($Profile -notin @("auto", "minimal")) {
        throw "CPU mode chi ho tro profile minimal"
    }
    $selectedProfile = "minimal"
}
$profileDiskGiB = @{ minimal = 25; balanced = 35; maximum = 55 }
$dataDrive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($context.NativeRoot))
$requiredDiskBytes = ([long]$profileDiskGiB[$selectedProfile]) * 1GB
if ($dataDrive.AvailableFreeSpace -lt $requiredDiskBytes) {
    throw "Profile $selectedProfile can it nhat $($profileDiskGiB[$selectedProfile]) GiB trong tren $($dataDrive.Name)"
}

Set-DubEnvValue -Path $envFile -Name "DUB_COMPUTE_MODE" -Value $selectedComputeMode
if ($selectedComputeMode -eq "gpu") {
    Set-DubEnvValue -Path $envFile -Name "CUDA_VISIBLE_DEVICES" -Value $gpu.Uuid
    Set-DubEnvValue -Path $envFile -Name "DUB_SELECTED_GPU_UUID" -Value $gpu.Uuid
    Set-DubEnvValue -Path $envFile -Name "DUB_SELECTED_CUDA_ARCHITECTURE" -Value ("sm_" + $gpu.Architecture)
    Set-DubEnvValue -Path $envFile -Name "DUB_SELECTED_CUDA_TOOLKIT_VERSION" -Value $cudaRelease
    Set-DubEnvValue -Path $envFile -Name "DUB_ASR_COMPUTE_TYPE" -Value "float16"
    Set-DubEnvValue -Path $envFile -Name "DUB_LLAMA_GPU_LAYERS" -Value "-1"
    Set-DubEnvValue -Path $envFile -Name "DUB_SEPARATION_CHUNK_SECONDS" -Value "120"
} else {
    Set-DubEnvValue -Path $envFile -Name "CUDA_VISIBLE_DEVICES" -Value ""
    Set-DubEnvValue -Path $envFile -Name "DUB_SELECTED_GPU_UUID" -Value ""
    Set-DubEnvValue -Path $envFile -Name "DUB_SELECTED_CUDA_ARCHITECTURE" -Value ""
    Set-DubEnvValue -Path $envFile -Name "DUB_SELECTED_CUDA_TOOLKIT_VERSION" -Value ""
    Set-DubEnvValue -Path $envFile -Name "DUB_ASR_COMPUTE_TYPE" -Value "int8"
    Set-DubEnvValue -Path $envFile -Name "DUB_LLAMA_GPU_LAYERS" -Value "0"
    Set-DubEnvValue -Path $envFile -Name "DUB_SEPARATION_CHUNK_SECONDS" -Value "30"
}
Set-ProfileDefaults -SelectedProfile $selectedProfile -EnvFile $envFile

$context = Initialize-DubWindowsEnvironment
Install-LlamaCpp -Context $context -Manifest $manifest -Gpu $gpu -ComputeMode $selectedComputeMode -EnvFile $envFile -Jobs $BuildJobs
$context = Initialize-DubWindowsEnvironment

$tiger = $manifest.components.tiger
$tigerTarget = Join-Path $context.NativeRoot "opt\tiger"
Sync-LockedRepository -Repository $tiger.repository -Commit $tiger.commit -Destination $tigerTarget
$overlay = Join-Path $projectRoot $tiger.compatibility_overlay
$overlayHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $overlay).Hash.ToLowerInvariant()
if ($overlayHash -ne $tiger.compatibility_overlay_sha256) {
    throw "TIGER compatibility overlay khong khop lock"
}
Copy-Item -LiteralPath $overlay -Destination (Join-Path $tigerTarget "look2hear\layers\__init__.py") -Force

$vieneu = $manifest.components.vieneu
$vieneuRoot = Join-Path $context.NativeRoot "opt\vieneu"
$vieneuSource = Join-Path $vieneuRoot "source"
Sync-LockedRepository -Repository $vieneu.repository -Commit $vieneu.commit -Destination $vieneuSource
[void](New-Item -ItemType Directory -Force -Path $vieneuRoot)
$entrypointSource = Join-Path $projectRoot $vieneu.entrypoint_source
$entrypointHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $entrypointSource).Hash.ToLowerInvariant()
if ($entrypointHash -ne $vieneu.entrypoint_sha256) {
    throw "VieNeu entrypoint khong khop lock"
}
Copy-Item -LiteralPath $entrypointSource -Destination $env:DUB_VIENEU_ENTRYPOINT -Force

if (-not (Test-Path -LiteralPath $context.VenvPython -PathType Leaf)) {
    Invoke-Checked $pythonExecutable "-m" "venv" $env:DUB_VENV_DIR
}
$python = $context.VenvPython
Invoke-Checked $python "-m" "pip" "install" "--disable-pip-version-check" "--upgrade" "pip"
$installedTorchRuntime = Invoke-DubNativeProbe -FilePath $python -Arguments @(
    "-c", "import torch; profile=('cu' + torch.version.cuda.replace('.', '')[:3] if torch.version.cuda else 'cpu'); print(profile + ';' + torch.__version__.split('+')[0])"
)
if ($selectedComputeMode -eq "gpu") {
    $torchProfile = "cu" + $cudaRelease.Replace(".", "")
} else {
    $torchProfile = "cpu"
}
if ($installedTorchRuntime -ne ($torchProfile + ";2.10.0")) {
    Invoke-Checked $python "-m" "pip" "uninstall" "-y" "torch"
    $torchIndex = "https://download.pytorch.org/whl/$torchProfile"
    $torchLock = Join-Path $projectRoot ("requirements\torch-" + $torchProfile + ".lock")
    Invoke-Checked $python "-m" "pip" "install" "--disable-pip-version-check" `
        "--index-url" $torchIndex "--extra-index-url" "https://pypi.org/simple" `
        "--require-hashes" "--requirement" $torchLock
}
Invoke-Checked $python "-m" "pip" "install" "--disable-pip-version-check" `
    "--require-hashes" "--requirement" (Join-Path $projectRoot "requirements\docker-gpu.lock")
Invoke-Checked $python "-m" "pip" "install" "--disable-pip-version-check" "--no-deps" "-e" $projectRoot
Invoke-Checked $python "-m" "pip" "check"
Invoke-Checked $python "-m" "compileall" "-q" (Join-Path $projectRoot "src")
if ($selectedComputeMode -eq "gpu") {
    Invoke-Checked $python "-c" "import torch; import ctranslate2, faster_whisper, onnxruntime, transformers; assert torch.cuda.is_available(); cap=torch.cuda.get_device_capability(0); arch=f'sm_{cap[0]}{cap[1]}'; assert arch in torch.cuda.get_arch_list(), (arch, torch.cuda.get_arch_list()); x=torch.ones((32,32), device='cuda', dtype=torch.float16); assert float((x@x)[0,0].cpu()) == 32.0"
} else {
    Invoke-Checked $python "-c" "import torch; import ctranslate2, faster_whisper, onnxruntime, transformers; assert not torch.cuda.is_available(); x=torch.ones((32,32), dtype=torch.float32); assert float((x@x)[0,0]) == 32.0; assert 'int8' in ctranslate2.get_supported_compute_types('cpu')"
}
Invoke-Checked $python "-m" "dub_server.worker" "--once"

if ($FullTest) {
    Invoke-Checked $python "-m" "pip" "install" "--disable-pip-version-check" "-e" ("${projectRoot}[test]")
    Invoke-Checked $python "-m" "pytest" "-q"
}
if (-not $SkipModels) {
    Invoke-Checked $python "-m" "dub_server.cli" "models" "install-profile" $selectedProfile "--yes"
}

& (Join-Path $PSScriptRoot "preflight.ps1") -RequireRuntime -ComputeMode $selectedComputeMode
if ($LASTEXITCODE -ne 0) {
    throw "Windows native preflight that bai"
}

Write-Output "Cai Windows native hoan tat"
if ($selectedComputeMode -eq "gpu") {
    Write-Output "GPU: $($gpu.Name), sm_$($gpu.Architecture), $($gpu.MemoryMiB) MiB"
} else {
    Write-Output "Compute: CPU compatibility mode (rat cham)"
}
Write-Output "Profile: $selectedProfile"
if (-not $SkipStart) {
    & (Join-Path $PSScriptRoot "stack.ps1") start
    if ($OpenDashboard) {
        Start-Process $env:DUB_API_URL
    }
} else {
    Write-Output "Khoi dong: .\windows\stack.ps1 start"
}
