Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

function Get-DubProjectRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Get-DubInstalledMemoryBytes {
    try {
        $memoryModules = @(Get-CimInstance Win32_PhysicalMemory -ErrorAction Stop)
        $installedBytes = [long](($memoryModules | Measure-Object -Property Capacity -Sum).Sum)
        if ($installedBytes -gt 0) {
            return $installedBytes
        }
    } catch {
        # Some managed Windows environments block Win32_PhysicalMemory. Fall back
        # to the OS-visible total so preflight can still report a useful result.
    }

    return [long](Get-CimInstance Win32_ComputerSystem -ErrorAction Stop).TotalPhysicalMemory
}

function Set-DubProcessEnvironmentDefault {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value
    )

    $current = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ([string]::IsNullOrWhiteSpace($current)) {
        [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
    }
}

function Import-DubEnvFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    foreach ($rawLine in [IO.File]::ReadAllLines($Path)) {
        $line = $rawLine.Trim()
        if ($line.Length -eq 0 -or $line.StartsWith("#")) {
            continue
        }
        $separator = $line.IndexOf("=")
        if ($separator -lt 1) {
            throw "Dong cau hinh Windows khong hop le: $rawLine"
        }
        $name = $line.Substring(0, $separator).Trim()
        $value = $line.Substring($separator + 1).Trim()
        if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
            throw "Ten bien moi truong Windows khong hop le: $name"
        }
        if ($value.Length -ge 2) {
            $first = $value.Substring(0, 1)
            $last = $value.Substring($value.Length - 1, 1)
            if (($first -eq ([string][char]34) -and $last -eq ([string][char]34)) -or
                ($first -eq "'" -and $last -eq "'")) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

function Initialize-DubWindowsEnvironment {
    $projectRoot = Get-DubProjectRoot
    $envFile = $env:DUB_WINDOWS_ENV_FILE
    if ([string]::IsNullOrWhiteSpace($envFile)) {
        $envFile = Join-Path $projectRoot ".env.windows"
    }
    Import-DubEnvFile -Path $envFile

    $nativeRoot = $env:DUB_NATIVE_ROOT
    if ([string]::IsNullOrWhiteSpace($nativeRoot)) {
        if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
            throw "Khong doc duoc LOCALAPPDATA de tao data root Windows"
        }
        $nativeRoot = Join-Path $env:LOCALAPPDATA "ThuyetMinhOfflineGPU"
    }
    if (-not [IO.Path]::IsPathRooted($nativeRoot)) {
        throw "DUB_NATIVE_ROOT phai la duong dan Windows tuyet doi"
    }
    $nativeRoot = [IO.Path]::GetFullPath($nativeRoot)

    Set-DubProcessEnvironmentDefault -Name "PROJECT_ROOT" -Value $projectRoot
    Set-DubProcessEnvironmentDefault -Name "DUB_PROJECT_ROOT" -Value $projectRoot
    Set-DubProcessEnvironmentDefault -Name "DUB_NATIVE_ROOT" -Value $nativeRoot
    Set-DubProcessEnvironmentDefault -Name "DUB_DATABASE_PATH" -Value (Join-Path $nativeRoot "state\jobs.sqlite3")
    Set-DubProcessEnvironmentDefault -Name "DUB_MODELS_LOCK_PATH" -Value (Join-Path $projectRoot "config\models.lock.json")
    Set-DubProcessEnvironmentDefault -Name "DUB_MODELS_DIR" -Value (Join-Path $nativeRoot "models")
    Set-DubProcessEnvironmentDefault -Name "DUB_INCOMING_DIR" -Value (Join-Path $nativeRoot "data\incoming")
    Set-DubProcessEnvironmentDefault -Name "DUB_JOBS_DIR" -Value (Join-Path $nativeRoot "data\jobs")
    Set-DubProcessEnvironmentDefault -Name "DUB_OUTPUT_DIR" -Value (Join-Path $nativeRoot "data\output")
    Set-DubProcessEnvironmentDefault -Name "DUB_GPU_REPORT_PATH" -Value (Join-Path $nativeRoot "state\gpu-health.json")
    Set-DubProcessEnvironmentDefault -Name "DUB_COMPUTE_MODE" -Value "gpu"
    Set-DubProcessEnvironmentDefault -Name "DUB_PROWLARR_API_KEY_FILE" -Value (Join-Path $nativeRoot "secrets\prowlarr_api_key")
    Set-DubProcessEnvironmentDefault -Name "DUB_QBITTORRENT_PASSWORD_FILE" -Value (Join-Path $nativeRoot "secrets\qbittorrent_password")
    Set-DubProcessEnvironmentDefault -Name "DUB_OPENSUBTITLES_API_KEY_FILE" -Value (Join-Path $nativeRoot "secrets\opensubtitles_api_key")
    Set-DubProcessEnvironmentDefault -Name "DUB_OPENSUBTITLES_TOKEN_FILE" -Value (Join-Path $nativeRoot "secrets\opensubtitles_token")
    Set-DubProcessEnvironmentDefault -Name "DUB_OPENSUBTITLES_BASE_URL_FILE" -Value (Join-Path $nativeRoot "secrets\opensubtitles_base_url")
    Set-DubProcessEnvironmentDefault -Name "DUB_RUNTIME_RUN_DIR" -Value (Join-Path $nativeRoot "run")
    Set-DubProcessEnvironmentDefault -Name "DUB_RUNTIME_LOG_DIR" -Value (Join-Path $nativeRoot "logs")
    Set-DubProcessEnvironmentDefault -Name "DUB_VENV_DIR" -Value (Join-Path $projectRoot ".venv-windows")
    Set-DubProcessEnvironmentDefault -Name "DUB_API_HOST" -Value "127.0.0.1"
    Set-DubProcessEnvironmentDefault -Name "DUB_API_ACCESS_POLICY" -Value "local"
    Set-DubProcessEnvironmentDefault -Name "DUB_API_PORT" -Value "8080"
    Set-DubProcessEnvironmentDefault -Name "DUB_API_URL" -Value "http://127.0.0.1:8080"
    Set-DubProcessEnvironmentDefault -Name "DUB_PROWLARR_URL" -Value "http://127.0.0.1:9696"
    Set-DubProcessEnvironmentDefault -Name "DUB_QBITTORRENT_URL" -Value "http://127.0.0.1:8081"
    Set-DubProcessEnvironmentDefault -Name "DUB_QBITTORRENT_USERNAME" -Value "dub"
    Set-DubProcessEnvironmentDefault -Name "DUB_DEFAULT_ASR_MODEL_ID" -Value "asr-faster-whisper-small"
    Set-DubProcessEnvironmentDefault -Name "DUB_DEFAULT_TRANSLATION_MODEL_ID" -Value "mt-gemma4-e2b-q4"
    Set-DubProcessEnvironmentDefault -Name "DUB_DEFAULT_SEPARATION_MODEL_ID" -Value "separation-tiger-dnr"
    Set-DubProcessEnvironmentDefault -Name "DUB_DEFAULT_TTS_MODEL_ID" -Value "tts-piper-vi-vais1000-medium"
    Set-DubProcessEnvironmentDefault -Name "DUB_TTS_SUPPORT_MODEL_ID" -Value "tts-neucodec-onnx-int8"
    Set-DubProcessEnvironmentDefault -Name "DUB_TIGER_SOURCE_DIR" -Value (Join-Path $nativeRoot "opt\tiger")
    Set-DubProcessEnvironmentDefault -Name "DUB_VIENEU_ENTRYPOINT" -Value (Join-Path $nativeRoot "opt\vieneu\vieneu-offline.py")
    Set-DubProcessEnvironmentDefault -Name "VIENEU_CODEC_PATH" -Value (Join-Path $nativeRoot "models\tts\support\neucodec-onnx-int8")
    Set-DubProcessEnvironmentDefault -Name "DUB_LLAMA_SERVER_BINARY" -Value (Join-Path $nativeRoot "opt\llama.cpp\llama-server.exe")
    Set-DubProcessEnvironmentDefault -Name "DUB_LLAMA_SERVER_PORT" -Value "18081"
    Set-DubProcessEnvironmentDefault -Name "DUB_LLAMA_GPU_LAYERS" -Value "-1"

    if ($env:DUB_API_HOST -ne "127.0.0.1") {
        throw "Windows MVP chi cho phep DUB_API_HOST=127.0.0.1"
    }
    $apiPort = 0
    if (-not [int]::TryParse($env:DUB_API_PORT, [ref]$apiPort) -or $apiPort -lt 1 -or $apiPort -gt 65535) {
        throw "DUB_API_PORT phai nam trong khoang 1..65535"
    }
    $expectedApiUrl = "http://127.0.0.1:$apiPort"
    if ($env:DUB_API_URL.TrimEnd("/") -ne $expectedApiUrl) {
        throw "DUB_API_URL phai la $expectedApiUrl"
    }

    if (-not [string]::IsNullOrWhiteSpace($env:CUDA_PATH)) {
        Set-DubProcessEnvironmentDefault -Name "CUDACXX" -Value (Join-Path $env:CUDA_PATH "bin\nvcc.exe")
    }

    $pythonPathParts = @(
        $env:DUB_TIGER_SOURCE_DIR,
        (Join-Path $nativeRoot "opt\vieneu\source\src")
    )
    if (-not [string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
        $pythonPathParts += $env:PYTHONPATH
    }
    $env:PYTHONPATH = $pythonPathParts -join [IO.Path]::PathSeparator

    $pathParts = @(
        (Join-Path $env:DUB_VENV_DIR "Scripts"),
        (Join-Path $env:DUB_VENV_DIR "Lib\site-packages\torch\lib"),
        (Join-Path $nativeRoot "opt\llama.cpp")
    )
    if (-not [string]::IsNullOrWhiteSpace($env:CUDA_PATH)) {
        $pathParts += (Join-Path $env:CUDA_PATH "bin")
    }
    $pathParts += $env:PATH
    $env:PATH = $pathParts -join [IO.Path]::PathSeparator

    return [PSCustomObject]@{
        ProjectRoot = $projectRoot
        EnvFile = $envFile
        NativeRoot = $nativeRoot
        VenvPython = (Join-Path $env:DUB_VENV_DIR "Scripts\python.exe")
        RunDirectory = $env:DUB_RUNTIME_RUN_DIR
        LogDirectory = $env:DUB_RUNTIME_LOG_DIR
    }
}

function New-DubWindowsDirectories {
    param([Parameter(Mandatory = $true)]$Context)

    $directories = @(
        (Split-Path -Parent $env:DUB_DATABASE_PATH),
        $env:DUB_MODELS_DIR,
        $env:DUB_INCOMING_DIR,
        $env:DUB_JOBS_DIR,
        $env:DUB_OUTPUT_DIR,
        (Split-Path -Parent $env:DUB_PROWLARR_API_KEY_FILE),
        $Context.RunDirectory,
        $Context.LogDirectory,
        (Join-Path $Context.NativeRoot "cache"),
        (Join-Path $Context.NativeRoot "opt")
    )
    foreach ($directory in $directories) {
        [void](New-Item -ItemType Directory -Force -Path $directory)
    }
}
