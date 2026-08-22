[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$context = Initialize-DubWindowsEnvironment
if (-not (Test-Path -LiteralPath $context.VenvPython -PathType Leaf)) {
    throw "Chua co runtime Windows; hay chay .\windows\install.ps1"
}
& $context.VenvPython -m dub_server.cli @Arguments
exit $LASTEXITCODE
